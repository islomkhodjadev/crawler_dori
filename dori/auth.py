"""Multi-admin authentication backed by the main SQLite database."""
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

LOGIN = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dori · Вход</title><body style="margin:0;background:#f4f6f5;font:15px system-ui,sans-serif;color:#18302c;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box"><form method="post" action="/api/login" style="width:100%;max-width:360px;padding:32px;background:#fff;border:1px solid #e2e8e5;border-radius:12px;display:flex;flex-direction:column;gap:12px"><h1 style="margin:0;font-size:22px">✳ Dori</h1><p style="margin:0;color:#6c7f7a">Войдите в панель · Panelga kiring · Sign in</p><input name="username" type="text" autocomplete="username" placeholder="Логин · Login" required style="padding:12px;border:1px solid #e2e8e5;border-radius:8px;font:inherit"><input name="password" type="password" autocomplete="current-password" placeholder="Пароль · Parol · Password" required style="padding:12px;border:1px solid #e2e8e5;border-radius:8px;font:inherit"><button style="padding:12px;border:0;border-radius:8px;background:#1d6b55;color:#fff;font:600 15px system-ui,sans-serif;cursor:pointer">Войти · Kirish · Sign in</button></form></body></html>'''


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


class AdminAuth:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Keep for backwards-compat checks in app.py that reference auth.password
        self.password = os.getenv('DORI_ADMIN_PASSWORD', '')
        self._attempts: dict = {}
        self._seed_default_user()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def _seed_default_user(self):
        """Create a default admin user on first run if none exist."""
        username = os.getenv('ADMIN_USERNAME', 'admin')
        password = (os.getenv('ADMIN_PASSWORD', '')
                    or os.getenv('DORI_ADMIN_PASSWORD', '')
                    or 'admin123')
        try:
            db = self._connect()
            count = db.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0]
            if count == 0:
                db.execute(
                    'INSERT INTO admin_users (id, username, password_hash, created_at) VALUES (?,?,?,?)',
                    (str(uuid.uuid4()), username, _hash(password), time.time())
                )
                db.commit()
            db.close()
        except Exception:
            pass  # Tables may not exist yet during very first migration

    def authorized(self, cookies) -> bool:
        token = cookies.get('dori_admin', '')
        if not token:
            return False
        db = self._connect()
        row = db.execute(
            'SELECT 1 FROM admin_sessions WHERE token=? AND expires_at>?',
            (token, time.time())
        ).fetchone()
        db.close()
        return row is not None

    def current_user(self, cookies) -> str | None:
        token = cookies.get('dori_admin', '')
        if not token:
            return None
        db = self._connect()
        row = db.execute(
            'SELECT username FROM admin_sessions WHERE token=? AND expires_at>?',
            (token, time.time())
        ).fetchone()
        db.close()
        return row['username'] if row else None

    def list_users(self) -> list:
        db = self._connect()
        rows = db.execute('SELECT id, username, created_at FROM admin_users ORDER BY created_at').fetchall()
        db.close()
        return [{'id': r['id'], 'username': r['username'], 'created_at': r['created_at']} for r in rows]

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError('username and password are required')
        db = self._connect()
        existing = db.execute('SELECT 1 FROM admin_users WHERE username=?', (username,)).fetchone()
        if existing:
            db.close()
            raise ValueError(f'User "{username}" already exists')
        uid = str(uuid.uuid4())
        db.execute(
            'INSERT INTO admin_users (id, username, password_hash, created_at) VALUES (?,?,?,?)',
            (uid, username, _hash(password), time.time())
        )
        db.commit()
        db.close()
        return {'id': uid, 'username': username}

    def delete_user(self, user_id: str, requesting_username: str) -> None:
        db = self._connect()
        row = db.execute('SELECT username FROM admin_users WHERE id=?', (user_id,)).fetchone()
        if not row:
            db.close()
            raise ValueError('User not found')
        if row['username'] == requesting_username:
            db.close()
            raise ValueError('Cannot delete your own account')
        count = db.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0]
        if count <= 1:
            db.close()
            raise ValueError('Cannot delete the last admin user')
        db.execute('DELETE FROM admin_users WHERE id=?', (user_id,))
        db.execute('DELETE FROM admin_sessions WHERE user_id=?', (user_id,))
        db.commit()
        db.close()

    def set_password(self, user_id: str, new_password: str) -> None:
        if not new_password:
            raise ValueError('password is required')
        db = self._connect()
        row = db.execute('SELECT 1 FROM admin_users WHERE id=?', (user_id,)).fetchone()
        if not row:
            db.close()
            raise ValueError('User not found')
        db.execute('UPDATE admin_users SET password_hash=? WHERE id=?', (_hash(new_password), user_id))
        db.execute('DELETE FROM admin_sessions WHERE user_id=?', (user_id,))
        db.commit()
        db.close()

    def agent_password(self):
        return os.getenv('DORI_AGENT_PASSWORD', '') or self.password

    def check_ws_password(self, password: str) -> bool:
        """Validate a plain-text password from the agent WebSocket query param.

        Uses DORI_AGENT_PASSWORD or DORI_ADMIN_PASSWORD env var if set.
        If neither is set, allows any connection (open-access mode).
        """
        agent_pw = self.agent_password()
        if not agent_pw:
            return True  # No password configured: open access
        return secrets.compare_digest(password.encode(), agent_pw.encode())

    def install(self, app):
        @app.middleware('http')
        async def protect(request, call_next):
            if request.url.path not in ('/login', '/api/login', '/api/agent/setup') and not self.authorized(request.cookies):
                if request.url.path.startswith('/api/'):
                    return JSONResponse({'detail': 'Admin sign-in required'}, status_code=401)
                return RedirectResponse('/login', status_code=303)
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['Referrer-Policy'] = 'same-origin'
            return response

        @app.get('/login')
        def login_page():
            return HTMLResponse(LOGIN)

        @app.post('/api/login')
        async def login(request: Request):
            who = request.client.host if request.client else 'unknown'
            moment = time.monotonic()
            failures = [t for t in self._attempts.get(who, []) if moment - t < 60]
            self._attempts[who] = failures
            if len(failures) >= 5:
                return JSONResponse({'detail': 'Too many login attempts. Try again in a minute.'}, status_code=429)

            form = await request.form()
            username = str(form.get('username', '')).strip()
            password = str(form.get('password', ''))

            db = self._connect()
            user = db.execute(
                'SELECT * FROM admin_users WHERE username=? AND password_hash=?',
                (username, _hash(password))
            ).fetchone()

            if not user:
                failures.append(moment)
                db.close()
                return HTMLResponse('Incorrect credentials. <a href="/login">Try again</a>', status_code=401)

            self._attempts.pop(who, None)
            token = secrets.token_urlsafe(32)
            expires = time.time() + 8 * 3600
            db.execute(
                'INSERT OR REPLACE INTO admin_sessions (token, user_id, username, expires_at) VALUES (?,?,?,?)',
                (token, user['id'], user['username'], expires)
            )
            # Clean up expired sessions
            db.execute('DELETE FROM admin_sessions WHERE expires_at<?', (time.time(),))
            db.commit()
            db.close()

            response = RedirectResponse('/', status_code=303)
            response.set_cookie('dori_admin', token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=8 * 3600)
            return response

        @app.post('/api/logout')
        async def logout(request: Request):
            token = request.cookies.get('dori_admin', '')
            if token:
                db = self._connect()
                db.execute('DELETE FROM admin_sessions WHERE token=?', (token,))
                db.commit()
                db.close()
            response = JSONResponse({'ok': True})
            response.delete_cookie('dori_admin')
            return response

        @app.get('/api/auth/me')
        async def me(request: Request):
            username = self.current_user(request.cookies)
            if not username:
                return JSONResponse({'detail': 'Not authenticated'}, status_code=401)
            return {'username': username}
