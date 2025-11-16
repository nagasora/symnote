# SymNote 🧠

「話す・書く・考える」を AI が整理する、ノートアプリ風の思考支援ツール。

- ノート / ToDo / 予定を日付ベースで保存
- カレンダー風 UI で「1日のログ」と「1週間のふりかえり」を確認
- AI による要約・キーワード抽出・関連アイデア提案（今後拡張）

## 開発環境

- Python 3.12
- Streamlit
- SQLite (ローカル DB)
- sentence-transformers / KeyBERT
- OpenAI API (任意：要約・週次ふりかえりで使用)

### セットアップ

```bash
git clone <this-repo>
cd symnote

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env           # 必要なら OPENAI_API_KEY を設定

# 開発用 起動
streamlit run src/symnote/app.py
