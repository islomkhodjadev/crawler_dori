"""Single-admin password sessions for the local panel and its remote browser."""
import os
import secrets
import time
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

LOGIN = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dori · Вход</title><body style="margin:0;background:#f4f6f5;font:15px system-ui,sans-serif;color:#18302c;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box"><form method="post" action="/api/login" style="width:100%;max-width:360px;padding:32px;background:#fff;border:1px solid #e2e8e5;border-radius:12px;display:flex;flex-direction:column;gap:12px"><h1 style="margin:0;font-size:22px">✳ Dori</h1><p style="margin:0;color:#6c7f7a">Войдите в панель · Panelga kiring · Sign in</p><input name="password" type="password" autocomplete="current-password" placeholder="Пароль · Parol · Password" required style="padding:12px;border:1px solid #e2e8e5;border-radius:8px;font:inherit"><button style="padding:12px;border:0;border-radius:8px;background:#1d6b55;color:#fff;font:600 15px system-ui,sans-serif;cursor:pointer">Войти · Kirish · Sign in</button></form></body></html>'''


class AdminAuth:
    def __init__(self):
        self.password = os.getenv('DORI_ADMIN_PASSWORD', '')
        self.sessions = {}
        self.attempts = {}

    def authorized(self, cookies):
        if not self.password:
            return True
        token = cookies.get('dori_admin', '')
        expiry = self.sessions.get(token, 0)
        return expiry > time.monotonic()

    def install(self, app):
        @app.middleware('http')
        async def protect(request, call_next):
            if request.url.path not in ('/login','/api/login') and not self.authorized(request.cookies):
                if request.url.path.startswith('/api/'):
                    return JSONResponse({'detail':'Admin sign-in required'}, status_code=401)
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
            failures = [t for t in self.attempts.get(who, []) if moment - t < 60]
            self.attempts[who] = failures
            if len(failures) >= 5:
                return JSONResponse({'detail':'Too many login attempts. Try again in a minute.'}, status_code=429)
            form = await request.form()
            if self.password and not secrets.compare_digest(str(form.get('password', '')).encode(), self.password.encode()):
                failures.append(moment)
                return HTMLResponse('Incorrect password. <a href="/login">Try again</a>', status_code=401)
            self.attempts.pop(who, None)
            self.sessions = {k:v for k,v in self.sessions.items() if v > moment}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = moment + 8 * 3600
            response = RedirectResponse('/', status_code=303)
            response.set_cookie('dori_admin', token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=8*3600)
            return response

        @app.post('/api/logout')
        async def logout(request: Request):
            self.sessions.pop(request.cookies.get('dori_admin', ''), None)
            response = JSONResponse({'ok':True})
            response.delete_cookie('dori_admin')
            return response
