"""本番デプロイ用 WSGI エントリポイント。

gunicorn wsgi:app で起動する。DBのテーブル作成は app.py の import 時に実行される。
"""

from app import app

if __name__ == "__main__":
    app.run()
