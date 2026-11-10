"""Single-admin password sessions for the local panel and its remote browser."""
import os
import secrets
import time
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

LOGIN = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dori admin login</title><body style="background:#f6f8f5;font:16px system-ui;color:#173a36;display:grid;place-items:center;min-height:90vh"><form method="post" action="/api/login" style="padding:40px;background:white;border-radius:12px"><h1>Dori admin</h1><p>Sign in to manage products and browser sessions.</p><input name="password" type="password" autocomplete="current-password" placeholder="Admin password" required style="padding:12px"><button style="padding:12px">Sign in</button></form></body></html>'''


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
