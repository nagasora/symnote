import Capacitor

final class SymNoteBridgeViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(SymNoteNativePlugin())
    }
}
