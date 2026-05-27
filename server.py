"""
UTE Server v4 — Auth, Pricing, Persistence, Rich Outputs
=========================================================
  GET  /                       → UI
  GET  /api/health             → status
  GET  /api/microsites         → public microsite list
  GET  /api/download/:id       → download output file
  GET  /api/chats              → user's chat list  [auth]
  GET  /api/chats/:id          → chat messages     [auth]
  GET  /api/me                 → current user      [auth]
  POST /api/signup             → create account
  POST /api/login              → authenticate
  POST /api/logout             → end session       [auth]
  POST /api/query              → run UTE           [auth or free]
  POST /api/draft-refinement   → auto-refine query [auth or free]
  POST /api/chats              → create new chat   [auth]
  DELETE /api/chats/:id        → delete chat       [auth]
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import engine
import auth
import outputs
import payment


def _token_from_request(handler) -> str:
    """Extract session token from Authorization header or cookie."""
    auth_header = handler.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    cookie = handler.headers.get('Cookie', '')
    for part in cookie.split(';'):
        k, _, v = part.strip().partition('=')
        if k.strip() == 'ute_token':
            return v.strip()
    return ''


class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a): pass

    def do_OPTIONS(self): self._send(200, b'', 'text/plain')

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')

        if path in ('', '/chat'):
            self._serve_file('static/index.html', 'text/html; charset=utf-8')

        elif path in ('/terms', '/terms.html'):
            self._serve_file('static/terms.html', 'text/html; charset=utf-8')

        elif path in ('/privacy', '/privacy.html'):
            self._serve_file('static/privacy.html', 'text/html; charset=utf-8')

        elif path == '/api/health':
            self._json({'status': 'ok', 'version': 'UTE v4'})

        elif path == '/api/microsites':
            self._list_microsites()

        elif path.startswith('/api/download/'):
            self._handle_download(path[len('/api/download/'):])

        elif path == '/api/memory':
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            try:
                from memory import get_user_summary
                self._json(get_user_summary(user['id']))
            except Exception as e:
                self._json({'error': str(e)}, 500)

        elif path == '/api/me':
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            lim = auth.check_limit(user)
            self._json({**user,
                        'limit': lim['limit'], 'used': lim['used'],
                        'tier_info': auth.TIERS.get(user['tier'], {})})

        elif path == '/api/chats':
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            # All users can see their chats
            # Pro users get full history; free users get last 5
            tier     = auth.TIERS.get(user['tier'], {})
            has_full = tier.get('history', False)
            chats    = auth.get_chats(user['id'])
            # Free users only see last 5 chats
            if not has_full:
                chats = chats[:5]
            self._json(chats)

        elif path.startswith('/api/chats/'):
            chat_id = path[len('/api/chats/'):]
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            self._json(auth.get_messages(chat_id, user['id']))

        elif path == '/api/pricing':
            self._json(auth.TIERS)

        elif path == '/api/plans':
            self._json(payment.PLANS)

        elif path == '/api/payments':
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            self._json(payment.get_payment_history(user['id']))

        else:
            self._send(404, b'Not found', 'text/plain')

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')

        if   path == '/api/signup':           self._handle_signup()
        elif path == '/api/login':            self._handle_login()
        elif path == '/api/logout':           self._handle_logout()
        elif path == '/api/query':            self._handle_query()
        elif path == '/api/draft-refinement': self._handle_draft_refinement()
        elif path == '/api/chats':            self._handle_create_chat()
        elif path == '/api/payment/order':    self._handle_payment_order()
        elif path == '/api/payment/verify':   self._handle_payment_verify()
        else: self._send(404, b'Not found', 'text/plain')

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip('/')
        if path == '/api/memory':
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            body = self._read_json()
            what = body.get('what', 'all')
            try:
                from memory import forget
                forget(user['id'], what)
                self._json({'ok': True, 'forgot': what})
            except Exception as e:
                self._json({'error': str(e)}, 500)
        elif path.startswith('/api/chats/'):
            chat_id = path[len('/api/chats/'):]
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            auth.delete_chat(chat_id, user['id'])
            self._json({'ok': True})
        else:
            self._send(404, b'Not found', 'text/plain')

    # ── Auth ──────────────────────────────────────────────────

    def _handle_signup(self):
        try:
            body = self._read_json()
            result = auth.signup(
                email    = body.get('email', ''),
                name     = body.get('name', ''),
                password = body.get('password', ''),
            )
            if result['ok']:
                self._set_cookie(result['token'])
            status = 200 if result['ok'] else 400
            self._json(result, status)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def _handle_login(self):
        try:
            body   = self._read_json()
            result = auth.login(body.get('email',''), body.get('password',''))
            if result['ok']:
                self._set_cookie(result['token'])
            status = 200 if result['ok'] else 401
            self._json(result, status)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def _handle_logout(self):
        token = _token_from_request(self)
        if token:
            auth.logout(token)
        self._json({'ok': True})

    # ── Query ─────────────────────────────────────────────────

    def _handle_query(self):
        try:
            body    = self._read_json()
            query   = (body.get('query') or '').strip()
            if not query:
                self._json({'error': 'empty query'}, 400); return

            history     = body.get('history') or []
            attachments = body.get('attachments') or []
            output_pref = body.get('output_type', 'auto')   # auto|pdf|docx|md|none
            chat_id     = body.get('chat_id', '')

            # Auth check — free users allowed, but with limit
            token = _token_from_request(self)
            user  = auth.get_user(token)

            if user:
                # Check query limit
                lim = auth.check_limit(user)
                if not lim['allowed']:
                    self._json({
                        'error': f"Daily limit reached ({lim['limit']} queries/day). "
                                 f"Upgrade to Pro for unlimited access.",
                        'upgrade': True,
                        'tiers': auth.TIERS,
                    }, 429)
                    return
                auth.increment_usage(user['id'])

                # Save user message to chat
                if not chat_id:
                    chat_id = auth.create_chat(user['id'], query[:60])
                auth.save_message(chat_id, user['id'], 'user', query)

            # Run UTE
            result = engine.run(
                query       = query,
                history     = history,
                attachments = attachments,
                user_id     = user.get('id', '') if user else '',
            )

            # Generate rich output file
            user_name = user['name'] if user else 'UTE'
            out = outputs.generate_output(
                query       = query,
                answer      = result.get('answer', ''),
                output_type = output_pref,
                user_name   = user_name,
            )
            if out:
                fid = engine._store_output_file(
                    out['name'], out['content'], out['mime']
                )
                result['output_file'] = {
                    'id':   fid,
                    'name': out['name'],
                    'mime': out['mime'],
                    'type': out['type'],
                }
            else:
                result['output_file'] = None

            # Save assistant message
            if user and chat_id:
                auth.save_message(chat_id, user['id'], 'assistant',
                                  result.get('answer', ''), result)
                result['chat_id'] = chat_id

            # Add usage info
            if user:
                lim2 = auth.check_limit(user)
                result['usage'] = {
                    'used': lim2['used'], 'limit': lim2['limit'],
                    'tier': user['tier'],
                }

            self._json(result)

        except Exception as e:
            self._json({'error': str(e)}, 500)

    def _handle_draft_refinement(self):
        try:
            body    = self._read_json()
            query   = (body.get('query') or '').strip()
            if not query:
                self._json({'error': 'empty query'}, 400); return
            drafted = engine.draft_refined_query(query, body.get('analysis') or {})
            self._json({'drafted_query': drafted})
        except Exception as e:
            self._json({'error': str(e)}, 500)

    # ── Chat management ───────────────────────────────────────

    def _handle_create_chat(self):
        user = auth.get_user(_token_from_request(self))
        if not user: self._json({'error': 'not authenticated'}, 401); return
        tier = auth.TIERS.get(user['tier'], {})
        if not tier.get('history'):
            self._json({'error': 'upgrade to Pro for chat history'}); return
        body    = self._read_json()
        chat_id = auth.create_chat(user['id'], body.get('title', ''))
        self._json({'chat_id': chat_id})

    # ── Payment ───────────────────────────────────────────────

    def _handle_payment_order(self):
        """Create a Razorpay order for a plan."""
        try:
            user = auth.get_user(_token_from_request(self))
            if not user: self._json({'error': 'not authenticated'}, 401); return
            body    = self._read_json()
            plan_id = body.get('plan_id', '')
            result  = payment.create_order(user['id'], plan_id)
            self._json(result, 200 if result.get('ok') else 400)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def _handle_payment_verify(self):
        """Verify Razorpay payment signature and activate plan."""
        try:
            body   = self._read_json()
            result = payment.verify_payment(
                razorpay_order_id   = body.get('razorpay_order_id', ''),
                razorpay_payment_id = body.get('razorpay_payment_id', ''),
                razorpay_signature  = body.get('razorpay_signature', ''),
            )
            if result.get('ok') and result.get('user_id'):
                # Return updated user info so UI can refresh
                token   = _token_from_request(self)
                updated = auth.get_user(token)
                if updated:
                    result['user'] = updated
                    lim = auth.check_limit(updated)
                    result['usage'] = {
                        'used': lim['used'], 'limit': lim['limit'],
                        'extra': lim.get('extra', 0), 'tier': updated['tier'],
                    }
            self._json(result, 200 if result.get('ok') else 400)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    # ── Files ─────────────────────────────────────────────────

    def _handle_download(self, file_id: str):
        rec = engine._OUTPUT_FILES.get(file_id.strip('/'))
        if not rec: self._send(404, b'File not found', 'text/plain'); return
        content = rec['content']
        self.send_response(200)
        self.send_header('Content-Type',        rec.get('mime','application/octet-stream'))
        self.send_header('Content-Length',      len(content))
        self.send_header('Content-Disposition', f'attachment; filename="{rec.get("name","download")}"')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(content)

    def _list_microsites(self):
        try:
            conn = engine._get_conn()
            rows = conn.execute(
                "SELECT id, query, entity, created_at, score, access_count "
                "FROM microsites ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            conn.close()
            self._json([dict(r) for r in rows])
        except Exception as e:
            self._json({'error': str(e)}, 500)

    # ── Helpers ───────────────────────────────────────────────

    def _read_json(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length) or b'{}')

    def _serve_file(self, rel_path: str, ctype: str):
        full = os.path.join(os.path.dirname(__file__), rel_path)
        if os.path.exists(full):
            with open(full, 'rb') as f:
                self._send(200, f.read(), ctype)
        else:
            self._send(404, b'Not found', 'text/plain')

    def _set_cookie(self, token: str):
        """Set session cookie — called before _json."""
        self._cookie_to_set = token

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        if hasattr(self, '_cookie_to_set') and self._cookie_to_set:
            self.send_header(
                'Set-Cookie',
                f'ute_token={self._cookie_to_set}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000'
            )
            self._cookie_to_set = ''
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header('Content-Type',   ctype)
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7532))
    os.makedirs(os.path.join(os.path.dirname(__file__), 'static'), exist_ok=True)
    print(f'UTE v4 running → http://localhost:{port}')
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
