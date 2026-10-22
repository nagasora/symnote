import AuthenticationServices
import Capacitor
import CryptoKit
import Foundation
import Security
import UIKit

@objc(SymNoteNativePlugin)
final class SymNoteNativePlugin: CAPPlugin, CAPBridgedPlugin, ASWebAuthenticationPresentationContextProviding {
    let identifier = "SymNoteNativePlugin"
    let jsName = "SymNoteNative"
    let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(#selector(authenticate(_:))),
        CAPPluginMethod(#selector(request(_:))),
        CAPPluginMethod(#selector(logout(_:)))
    ]

    private static let expectedAPIOrigin = "https://app.symnote.app"
    private static let callbackScheme = "app.symnote.mobile"
    private static let keychainService = "app.symnote.mobile.authentication"
    private static let keychainAccount = "symnote-session"
    private static let allowedMethods = Set(["GET", "POST", "PUT", "PATCH", "DELETE"])
    private static let blockedAuthPaths = [
        "/api/auth/mobile",
        "/api/auth/google"
    ]

    private var authenticationSession: ASWebAuthenticationSession?
    private var authenticationInProgress = false
    private var logoutInProgress = false

    @objc func authenticate(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self else {
                call.reject("Authentication is unavailable.", "AUTH_UNAVAILABLE")
                return
            }
            guard !self.authenticationInProgress else {
                call.reject("Authentication is already in progress.", "AUTH_IN_PROGRESS")
                return
            }
            guard !self.logoutInProgress else {
                call.reject("Sign out is in progress.", "LOGOUT_IN_PROGRESS")
                return
            }

            do {
                let verifier = try Self.randomBase64URL(byteCount: 32)
                let appState = try Self.randomBase64URL(byteCount: 32)
                let challengeDigest = SHA256.hash(data: Data(verifier.utf8))
                let challenge = Self.base64URL(Data(challengeDigest))
                let startURL = try self.authenticationStartURL(challenge: challenge, appState: appState)

                self.authenticationInProgress = true
                let session = ASWebAuthenticationSession(
                    url: startURL,
                    callbackURLScheme: Self.callbackScheme
                ) { [weak self] callbackURL, error in
                    DispatchQueue.main.async {
                        guard let self else {
                            call.reject("Authentication failed.", "AUTH_FAILED")
                            return
                        }
                        self.authenticationSession = nil

                        if let authenticationError = error as? ASWebAuthenticationSessionError,
                           authenticationError.code == .canceledLogin {
                            self.finishAuthentication(call, message: "Authentication was cancelled.", code: "AUTH_CANCELLED")
                            return
                        }
                        guard error == nil, let callbackURL else {
                            self.finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
                            return
                        }

                        do {
                            let code = try self.validatedAuthorizationCode(from: callbackURL, expectedState: appState)
                            self.exchangeAuthorizationCode(code, verifier: verifier, call: call)
                        } catch let callbackError as CallbackError {
                            switch callbackError {
                            case .cancelled:
                                self.finishAuthentication(call, message: "Authentication was cancelled.", code: "AUTH_CANCELLED")
                            case .notInvited:
                                self.finishAuthentication(call, message: "This account cannot access SymNote.", code: "AUTH_NOT_INVITED")
                            case .invalid, .failed:
                                self.finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
                            }
                        } catch {
                            self.finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
                        }
                    }
                }
                session.presentationContextProvider = self
                session.prefersEphemeralWebBrowserSession = true
                self.authenticationSession = session

                guard session.start() else {
                    self.authenticationSession = nil
                    self.authenticationInProgress = false
                    call.reject("Authentication is unavailable.", "AUTH_UNAVAILABLE")
                    return
                }
            } catch {
                self.authenticationSession = nil
                self.authenticationInProgress = false
                call.reject("Authentication is unavailable.", "AUTH_UNAVAILABLE")
            }
        }
    }

    @objc func request(_ call: CAPPluginCall) {
        guard let path = call.getString("path"),
              let methodValue = call.getString("method") else {
            call.reject("A valid API path and method are required.", "INVALID_REQUEST")
            return
        }

        let method = methodValue.uppercased()
        guard Self.allowedMethods.contains(method) else {
            call.reject("The requested API method is not allowed.", "METHOD_NOT_ALLOWED")
            return
        }

        let url: URL
        do {
            url = try validatedAPIURL(path: path, allowAuthEndpoints: false)
        } catch {
            call.reject("The requested API path is not allowed.", "PATH_NOT_ALLOWED")
            return
        }

        let token: String?
        do {
            token = try Self.readSessionToken()
            if token == nil && !(method == "GET" && url.path == "/api/auth/me") {
                call.reject("Authentication is required.", "NOT_AUTHENTICATED")
                return
            }
        } catch {
            call.reject("Authentication is unavailable.", "AUTH_UNAVAILABLE")
            return
        }

        var bodyData: Data?
        if let suppliedBody = call.getValue("body") {
            guard method != "GET",
                  let body = suppliedBody as? JSObject,
                  JSONSerialization.isValidJSONObject(body) else {
                call.reject("The request body is invalid.", "INVALID_REQUEST")
                return
            }
            do {
                bodyData = try JSONSerialization.data(withJSONObject: body)
            } catch {
                call.reject("The request body is invalid.", "INVALID_REQUEST")
                return
            }
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = bodyData
        request.timeoutInterval = 30
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue("1", forHTTPHeaderField: "X-SymNote")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if bodyData != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let session = Self.makeEphemeralSession()
        session.dataTask(with: request) { data, response, error in
            defer { session.finishTasksAndInvalidate() }
            guard error == nil,
                  let response = response as? HTTPURLResponse,
                  let responseURL = response.url,
                  Self.isExpectedOrigin(responseURL) else {
                call.reject("The API request failed.", "NETWORK_ERROR")
                return
            }

            let responseObject: [String: Any]
            if let data, !data.isEmpty {
                guard let object = try? JSONSerialization.jsonObject(with: data),
                      let dictionary = object as? [String: Any] else {
                    call.reject("The API returned an invalid response.", "INVALID_RESPONSE")
                    return
                }
                responseObject = dictionary
            } else {
                responseObject = [:]
            }

            call.resolve([
                "status": response.statusCode,
                "data": responseObject
            ])
        }.resume()
    }

    @objc func logout(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self else {
                call.reject("Sign out failed.", "LOGOUT_FAILED")
                return
            }
            guard !self.logoutInProgress else {
                call.reject("Sign out is already in progress.", "LOGOUT_IN_PROGRESS")
                return
            }
            guard !self.authenticationInProgress else {
                call.reject("Authentication is in progress.", "AUTH_IN_PROGRESS")
                return
            }
            self.logoutInProgress = true
            self.performLogout(call)
        }
    }

    private func performLogout(_ call: CAPPluginCall) {
        let token: String?
        do {
            token = try Self.readSessionToken()
        } catch {
            logoutInProgress = false
            call.reject("Sign out failed.", "LOGOUT_FAILED")
            return
        }

        guard let token else {
            do {
                try Self.deleteSessionToken()
                logoutInProgress = false
                notifyListeners("symnote-native-session-changed", data: ["authenticated": false])
                call.resolve(["authenticated": false, "remoteRevoked": true])
            } catch {
                logoutInProgress = false
                call.reject("Sign out failed.", "LOGOUT_FAILED")
            }
            return
        }

        let logoutURL: URL
        do {
            logoutURL = try validatedAPIURL(path: "/api/auth/logout", allowAuthEndpoints: true)
        } catch {
            logoutInProgress = false
            call.reject("Sign out failed.", "LOGOUT_FAILED")
            return
        }

        var request = URLRequest(url: logoutURL)
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("1", forHTTPHeaderField: "X-SymNote")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let session = Self.makeEphemeralSession()
        session.dataTask(with: request) { _, response, _ in
            defer { session.finishTasksAndInvalidate() }
            let remoteRevoked: Bool
            if let httpResponse = response as? HTTPURLResponse,
               let responseURL = httpResponse.url,
               Self.isExpectedOrigin(responseURL) {
                remoteRevoked = (200..<300).contains(httpResponse.statusCode)
            } else {
                remoteRevoked = false
            }

            do {
                try Self.deleteSessionToken()
                DispatchQueue.main.async {
                    self.logoutInProgress = false
                    self.notifyListeners("symnote-native-session-changed", data: ["authenticated": false])
                    call.resolve([
                        "authenticated": false,
                        "remoteRevoked": remoteRevoked
                    ])
                }
            } catch {
                DispatchQueue.main.async {
                    self.logoutInProgress = false
                    call.reject("Sign out failed.", "LOGOUT_FAILED")
                }
            }
        }.resume()
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        if let window = bridge?.viewController?.viewIfLoaded?.window {
            return window
        }
        if let windowScene = bridge?.viewController?.view.window?.windowScene {
            return ASPresentationAnchor(windowScene: windowScene)
        }
        return ASPresentationAnchor()
    }

    private func authenticationStartURL(challenge: String, appState: String) throws -> URL {
        let origin = try apiOrigin()
        var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)
        components?.path = "/api/auth/mobile/start"
        components?.queryItems = [
            URLQueryItem(name: "challenge", value: challenge),
            URLQueryItem(name: "app_state", value: appState)
        ]
        guard let url = components?.url, Self.isExpectedOrigin(url) else {
            throw NativeError.invalidConfiguration
        }
        return url
    }

    private func validatedAuthorizationCode(from url: URL, expectedState: String) throws -> String {
        guard url.scheme?.lowercased() == Self.callbackScheme,
              url.host?.lowercased() == "auth",
              url.path == "/callback",
              url.user == nil,
              url.password == nil,
              url.port == nil,
              url.fragment == nil,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw CallbackError.invalid
        }

        let queryItems = components.queryItems ?? []
        let allowedNames = Set(["code", "app_state", "error"])
        guard queryItems.allSatisfy({ allowedNames.contains($0.name) }) else {
            throw CallbackError.invalid
        }

        func singleValue(_ name: String) -> String? {
            let matching = queryItems.filter { $0.name == name }
            guard matching.count == 1 else { return nil }
            return matching[0].value
        }

        guard let callbackState = singleValue("app_state"),
              callbackState == expectedState else {
            throw CallbackError.invalid
        }

        if let callbackError = singleValue("error") {
            guard singleValue("code") == nil else { throw CallbackError.invalid }
            switch callbackError {
            case "cancelled":
                throw CallbackError.cancelled
            case "not_invited":
                throw CallbackError.notInvited
            case "failed":
                throw CallbackError.failed
            default:
                throw CallbackError.invalid
            }
        }

        guard let code = singleValue("code"),
              !code.isEmpty,
              code.count <= 2048 else {
            throw CallbackError.invalid
        }
        return code
    }

    private func exchangeAuthorizationCode(_ code: String, verifier: String, call: CAPPluginCall) {
        let exchangeURL: URL
        do {
            exchangeURL = try validatedAPIURL(path: "/api/auth/mobile/exchange", allowAuthEndpoints: true)
        } catch {
            finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
            return
        }

        var request = URLRequest(url: exchangeURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("1", forHTTPHeaderField: "X-SymNote")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "code": code,
                "verifier": verifier
            ])
        } catch {
            finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
            return
        }

        let session = Self.makeEphemeralSession()
        session.dataTask(with: request) { [weak self] data, response, error in
            defer { session.finishTasksAndInvalidate() }
            guard let self else {
                call.reject("Authentication failed.", "AUTH_FAILED")
                return
            }
            guard error == nil,
                  let response = response as? HTTPURLResponse,
                  response.statusCode == 200,
                  let responseURL = response.url,
                  Self.isExpectedOrigin(responseURL),
                  let data,
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let dictionary = object as? [String: Any],
                  let token = dictionary["session_token"] as? String,
                  !token.isEmpty else {
                self.finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
                return
            }

            do {
                try Self.storeSessionToken(token)
                DispatchQueue.main.async {
                    self.authenticationInProgress = false
                    self.notifyListeners("symnote-native-session-changed", data: ["authenticated": true])
                    call.resolve(["authenticated": true])
                }
            } catch {
                self.finishAuthentication(call, message: "Authentication failed.", code: "AUTH_FAILED")
            }
        }.resume()
    }

    private func finishAuthentication(_ call: CAPPluginCall, message: String, code: String) {
        DispatchQueue.main.async { [weak self] in
            self?.authenticationSession?.cancel()
            self?.authenticationSession = nil
            self?.authenticationInProgress = false
            call.reject(message, code)
        }
    }

    private func validatedAPIURL(path: String, allowAuthEndpoints: Bool) throws -> URL {
        guard !path.isEmpty,
              path.count <= 4096,
              path.hasPrefix("/api/"),
              !path.hasPrefix("//"),
              !path.contains("\\"),
              !path.contains("#"),
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let relativeComponents = URLComponents(string: path),
              relativeComponents.scheme == nil,
              relativeComponents.host == nil,
              relativeComponents.user == nil,
              relativeComponents.password == nil,
              relativeComponents.port == nil,
              relativeComponents.fragment == nil else {
            throw NativeError.invalidPath
        }

        let encodedPath = relativeComponents.percentEncodedPath
        guard encodedPath.hasPrefix("/api/"),
              !encodedPath.contains("%"),
              !relativeComponents.path.contains("//") else {
            throw NativeError.invalidPath
        }

        let segments = relativeComponents.path.split(separator: "/", omittingEmptySubsequences: false)
        guard !segments.contains(where: { $0 == "." || $0 == ".." }) else {
            throw NativeError.invalidPath
        }

        if !allowAuthEndpoints {
            let requestPath = relativeComponents.path
            if Self.blockedAuthPaths.contains(where: {
                requestPath == $0 || requestPath.hasPrefix($0 + "/")
            }) || requestPath == "/api/auth/logout" {
                throw NativeError.invalidPath
            }
        }

        let origin = try apiOrigin()
        var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)
        components?.percentEncodedPath = encodedPath
        components?.percentEncodedQuery = relativeComponents.percentEncodedQuery
        guard let url = components?.url, Self.isExpectedOrigin(url) else {
            throw NativeError.invalidPath
        }
        return url
    }

    private func apiOrigin() throws -> URL {
        guard let configuredOrigin = Bundle.main.object(forInfoDictionaryKey: "SymNoteAPIOrigin") as? String,
              configuredOrigin == Self.expectedAPIOrigin,
              let url = URL(string: Self.expectedAPIOrigin),
              Self.isExpectedOrigin(url) else {
            throw NativeError.invalidConfiguration
        }
        return url
    }

    private static func isExpectedOrigin(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https"
            && url.host?.lowercased() == "app.symnote.app"
            && url.port == nil
            && url.user == nil
            && url.password == nil
    }

    private static func randomBase64URL(byteCount: Int) throws -> String {
        var bytes = [UInt8](repeating: 0, count: byteCount)
        let status = bytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, byteCount, buffer.baseAddress!)
        }
        guard status == errSecSuccess else {
            throw NativeError.randomGenerationFailed
        }
        return base64URL(Data(bytes))
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func storeSessionToken(_ token: String) throws {
        guard let tokenData = token.data(using: .utf8) else {
            throw NativeError.keychainFailure
        }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: keychainAccount
        ]
        let updates: [String: Any] = [
            kSecValueData as String: tokenData,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, updates as CFDictionary)
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw NativeError.keychainFailure
        }

        var addQuery = query
        updates.forEach { addQuery[$0.key] = $0.value }
        guard SecItemAdd(addQuery as CFDictionary, nil) == errSecSuccess else {
            throw NativeError.keychainFailure
        }
    }

    private static func readSessionToken() throws -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: keychainAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8),
              !token.isEmpty else {
            throw NativeError.keychainFailure
        }
        return token
    }

    private static func deleteSessionToken() throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: keychainAccount
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NativeError.keychainFailure
        }
    }

    private static func makeEphemeralSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 45
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration, delegate: NoRedirectSessionDelegate.shared, delegateQueue: nil)
    }
}

private enum NativeError: Error {
    case invalidConfiguration
    case invalidPath
    case keychainFailure
    case randomGenerationFailed
}

private enum CallbackError: Error {
    case invalid
    case cancelled
    case failed
    case notInvited
}

private final class NoRedirectSessionDelegate: NSObject, URLSessionTaskDelegate {
    static let shared = NoRedirectSessionDelegate()

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
