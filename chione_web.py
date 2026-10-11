#!/usr/bin/env python3
"""Local web settings panel for the Chione display (browser version of the
GNOME app). Listens only on 127.0.0.1 and writes the same settings file that
the chione-temp user service reloads within a second.
"""
import argparse
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import chione_core as core
import chione_gui as g

LOCK = threading.Lock()
MAX_BODY = 64 * 1024
ALLOWED_KEYS = ("mode", "cycle", "fahrenheit", "alarm", "fan_hwmon",
                "fan_input")

# ---------------------------------------------------------------- logic


def _load(path):
    """(settings, error message or None); {} when the file is unreadable."""
    try:
        return core.load_settings(path), None
    except (ValueError, OSError) as exc:
        return {}, str(exc)


def _store(path):
    settings, error = _load(path)
    state = g.initial_state(settings, g.read_conf_args())
    return g.Store(path, state, settings), error


def api_state(path):
    store, error = _store(path)
    cpu, rpm = g.read_live(store.state)
    installed = g.service_installed()
    return {"state": store.state, "fans": g.scan_fans(),
            "live": {"cpu": cpu, "rpm": rpm},
            "service": {"installed": installed,
                        "active": g.service_active() if installed else False,
                        "hint": g.service_hint()},
            "settings_error": error, "default_alarm": g.DEFAULT_ALARM}


def api_update(path, changes):
    """Apply changes. Raises ValueError (bad input) or OSError (write)."""
    if not isinstance(changes, dict):
        raise ValueError("esperado um objeto JSON")  # noqa: TRY004
    unknown = sorted(set(changes) - set(ALLOWED_KEYS))
    if unknown:
        raise ValueError("chave desconhecida: " + ", ".join(unknown))
    with LOCK:
        store, error = _store(path)
        if error:
            # Never overwrite a file we could not read.
            raise FileExistsError(
                "arquivo de ajustes inválido, corrija ou apague: " + error)
        store.update(**changes)
        return {"state": store.state}


def api_service(action):
    if action not in ("start", "stop"):
        raise ValueError("ação inválida")
    # Same as the GNOME app: enable/disable --now.
    with LOCK:
        ok, _ = g.systemctl("enable" if action == "start" else "disable",
                            "--now", g.SERVICE)
    if not ok:
        raise OSError("Não foi possível alterar o serviço")
    return {"active": g.service_active()}


def host_allowed(host, port):
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def token_ok(expected, given):
    return bool(given) and secrets.compare_digest(expected, given)


# ----------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chione Display</title>
<style>
:root{color-scheme:dark;--bg:#000;--fg:#fff;--mut:#a3a3a3;--line:rgba(255,255,255,.14);--glass:rgba(255,255,255,.08);--r:40px;
--slab:linear-gradient(160deg,#141414,#050505);
--rim:inset 0 1px 0 rgba(255,255,255,.28),inset 0 0 0 1px rgba(255,255,255,.12),inset 0 0 28px rgba(255,255,255,.06),0 0 32px rgba(255,255,255,.07)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 "Inter","Helvetica Neue",Helvetica,Arial,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:560px;margin:0 auto;padding:16px 16px 96px}
.hero{position:relative;display:flex;flex-direction:column;align-items:center;gap:20px;padding:34px 20px 26px;margin-bottom:8px;overflow:hidden}
.slab,.hero{background:var(--slab);border-radius:var(--r);box-shadow:var(--rim)}
.circle{flex:none;width:min(260px,80%);aspect-ratio:1;border-radius:50%;background:#fff;color:#000;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 12px;box-shadow:0 0 60px rgba(255,255,255,.25)}
.circle .big{font-size:clamp(1.6rem,9vw,2.6rem);font-weight:700;letter-spacing:-.03em;line-height:1.1}
.circle small{display:block;font-size:.7rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:#444;margin-bottom:4px}
.bar{align-self:stretch;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 20px;border-radius:999px;background:linear-gradient(180deg,rgba(0,0,0,.5),rgba(0,0,0,.66));border:1px solid rgba(255,255,255,.4);box-shadow:inset 0 1px 0 rgba(255,255,255,.5),inset 0 0 24px rgba(255,255,255,.08);-webkit-backdrop-filter:blur(22px) saturate(1.2);backdrop-filter:blur(22px) saturate(1.2);text-shadow:0 1px 3px rgba(0,0,0,.6)}
.bar span:first-child{font-size:.75rem;letter-spacing:.14em;text-transform:uppercase;color:#eee}
.bar .big{font-size:1.1rem}
.foot{align-self:stretch;margin:8px 4px 0;display:flex;justify-content:space-between;align-items:flex-end;gap:12px}
.hero h1{margin:0;min-width:0;font-size:clamp(1.7rem,9.5vw,4rem);font-weight:600;letter-spacing:-.04em;line-height:1}
.badge{flex:none;margin-bottom:6px;display:flex;align-items:center;gap:8px;border:1px solid #fff;border-radius:999px;padding:6px 12px;font-size:.58rem;line-height:1.25;letter-spacing:.18em;text-transform:uppercase}
.badge b{display:inline-block;width:18px;height:1px;background:#fff;vertical-align:middle;margin-right:4px}
.badge i{font-style:normal;font-size:.9rem;letter-spacing:0;white-space:nowrap}
h2{font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.18em;color:var(--mut);margin:32px 8px 10px}
.card{background:var(--slab);border-radius:var(--r);box-shadow:var(--rim);padding:8px 12px;overflow:hidden}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 12px;border-top:1px solid var(--line);flex-wrap:wrap;transition:opacity .2s}
.row:first-child{border-top:0}
.row.off{opacity:.55}
.hint{display:block;font-size:.75rem;color:var(--mut);margin-top:2px}
#fan{min-width:12rem;max-width:60%;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
@media(max-width:480px){#fan{width:100%;max-width:100%}.badge{padding:5px 10px;letter-spacing:.12em}}
@media(max-width:360px){.circle small{font-size:.6rem;letter-spacing:.1em}.badge b{display:none}.foot{margin:8px 0 0}}
body:has(#offline:not([hidden])) main>:not(.banner){opacity:.55;pointer-events:none;transition:opacity .2s}
input[type=number]::placeholder{color:var(--mut)}
select,input[type=number],button{font:inherit;color:var(--fg);background:var(--glass);border:1px solid rgba(255,255,255,.25);border-radius:999px;padding:8px 16px;max-width:100%;-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
select{color-scheme:dark}
input[type=number]{width:6.5em;text-align:center}
button{cursor:pointer;background:#fff;color:#000;font-weight:600;border-color:#fff;transition:background .2s,transform .2s}
button#log{background:transparent;color:#fff;border-color:#fff}
button:hover{background:#d9d9d9}
button#log:hover{background:rgba(255,255,255,.14)}
button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid #fff;outline-offset:3px}
.big{font-size:1.5rem;font-weight:600;font-variant-numeric:tabular-nums}
.banner{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.3);color:#fff;border-radius:999px;padding:10px 20px;margin-bottom:12px;font-size:.9rem;-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px)}
@media(max-width:480px){.banner{border-radius:28px}}
.banner[hidden]{display:none}
#offline{text-align:center;font-weight:600}
code{font-family:ui-monospace,monospace;word-break:break-all}
pre{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word;font-size:.8rem;max-height:50vh;overflow:auto;color:#ddd}
input[type=checkbox]{appearance:none;-webkit-appearance:none;position:relative;width:52px;height:30px;margin:0;border-radius:999px;background:transparent;border:1px solid #fff;cursor:pointer;transition:background .2s}
input[type=checkbox]::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .2s,background .2s}
input[type=checkbox]:checked{background:#fff}
input[type=checkbox]:checked::after{background:#000;transform:translateX(22px)}
input[type=checkbox]:disabled{opacity:.4;cursor:default}
#toast{position:fixed;left:50%;width:max-content;text-align:center;bottom:20px;transform:translateX(-50%);max-width:calc(100% - 32px);background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.4);padding:10px 20px;border-radius:999px;opacity:0;transition:opacity .2s;pointer-events:none;-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px)}
#toast.show{opacity:1}
#toast.err{background:#fff;color:#000;font-weight:600}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.bar,.banner,#toast,select,input[type=number]{background:#2a2a2a}.bar{background:#555}}
@media(prefers-reduced-motion:reduce){*,::after{transition:none!important}}
</style></head><body><main>
<div id="offline" class="banner" hidden>sem conexão com o painel</div>
<div id="banner" class="banner" hidden></div>
<section class="hero" aria-label="Leitura ao vivo">
<div class="circle"><div><small>Temperatura da CPU</small><span class="big" id="cpu">-</span></div></div>
<div class="bar"><span>Fan</span><span class="big" id="rpm">-</span></div>
<div class="foot"><h1>Chione</h1>
<div class="badge" aria-hidden="true"><i><b></b>&#10022;</i><span>Chione<br>Display</span></div></div>
</section>
<h2>Exibição</h2>
<div class="card">
<div class="row"><label for="mode">Modo</label><select id="mode">
<option value="cpu">CPU</option><option value="fan">Fan</option><option value="auto">Alternar</option></select></div>
<div class="row" id="cycle-row"><label for="cycle">Trocar a cada (s)</label><input type="number" id="cycle" min="1" max="60" step="1" placeholder="..."></div>
<div class="row"><label for="unit">Unidade</label><select id="unit"><option value="c">°C</option><option value="f">°F</option></select></div>
</div>
<h2>Alarme</h2>
<div class="card">
<div class="row"><label for="alarm-on">Piscar acima de</label><input type="checkbox" id="alarm-on"></div>
<div class="row" id="alarm-row"><label for="alarm">Limite (°C)<span class="hint">sempre em °C</span></label><input type="number" id="alarm" min="30" max="120" step="1" placeholder="..."></div>
</div>
<h2>Fan</h2>
<div class="card"><div class="row"><label for="fan">Fonte do fan</label><select id="fan"></select></div></div>
<h2>Serviço</h2>
<div class="card">
<div class="row"><label for="svc">Serviço ativo</label><input type="checkbox" id="svc"></div>
<div class="row"><button id="test">Testar display</button><button id="log">Ver log</button></div>
<pre id="logbox" hidden></pre>
</div>
</main><div id="toast"></div>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
let defAlarm=90,seq=0,shown=0,timers={},toastT;
function toast(msg,err){const t=$("toast");t.textContent=msg;t.className="show"+(err?" err":"");clearTimeout(toastT);toastT=setTimeout(()=>t.className="",2200)}
async function api(path,body){
  const opt=body===undefined?{headers:{"X-Chione-Token":TOKEN}}:{method:"POST",headers:{"Content-Type":"application/json","X-Chione-Token":TOKEN},body:JSON.stringify(body)};
  const r=await fetch(path,opt);const j=await r.json().catch(()=>({}));
  if(!r.ok){const e=new Error(j.error||("erro "+r.status));e.status=r.status;throw e}return j}
function fanLabel(f){let t=f.hwmon+" "+f.file.replace("_input","")+" · "+(f.rpm===null?"n/d":f.rpm+" RPM");return f.gpu?t+" (GPU)":t}
function temp(c,f){return c===null?"sem sensor":f?(c*9/5+32).toFixed(1)+" °F":c.toFixed(1)+" °C"}
function fill(d){
  const s=d.state;defAlarm=d.default_alarm;
  const b=$("banner");
  if(!d.service.installed){b.hidden=false;b.innerHTML="Serviço não instalado. Para instalar: <code></code>";b.querySelector("code").textContent=d.service.hint}
  else if(d.settings_error){b.hidden=false;b.textContent="Erro no arquivo de ajustes: "+d.settings_error}
  else b.hidden=true;
  const a=document.activeElement;
  const set=(el,fn)=>{if(a!==el)fn()};
  set($("mode"),()=>$("mode").value=s.mode);
  set($("cycle"),()=>$("cycle").value=s.cycle);
  $("cycle").disabled=s.mode!=="auto";$("cycle-row").classList.toggle("off",s.mode!=="auto");
  set($("unit"),()=>$("unit").value=s.fahrenheit?"f":"c");
  set($("alarm-on"),()=>$("alarm-on").checked=s.alarm!==null);
  set($("alarm"),()=>$("alarm").value=s.alarm===null?defAlarm:s.alarm);
  $("alarm").disabled=s.alarm===null;$("alarm-row").classList.toggle("off",s.alarm===null);
  const sel=$("fan");
  if(a!==sel){sel.innerHTML="";
    const o=document.createElement("option");o.value="";o.textContent="Automático";sel.appendChild(o);
    d.fans.forEach((f,i)=>{const e=document.createElement("option");e.value=f.hwmon+"|"+f.file;e.textContent=fanLabel(f);sel.appendChild(e)});
    const k=s.fan_hwmon+"|"+s.fan_input;sel.value=d.fans.some(f=>f.hwmon+"|"+f.file===k)?k:""}
  set($("svc"),()=>{$("svc").checked=d.service.active});
  $("svc").disabled=!d.service.installed;
  live(d)}
function live(d){$("cpu").textContent=temp(d.live.cpu,d.state.fahrenheit);$("rpm").textContent=d.live.rpm===null?"sem sensor":d.live.rpm+" RPM"}
async function refresh(){
  const n=++seq;
  try{const d=await api("/api/state");if(n<shown)return;shown=n;$("offline").hidden=true;fill(d)}
  catch(e){if(n>=shown){$("offline").textContent=e.status===403?"Painel reiniciado: abra o endereço novo mostrado no terminal":"sem conexão com o painel";$("offline").hidden=false}}}
async function save(ch){
  try{await api("/api/settings",ch);toast("Aplicado");refresh()}
  catch(e){toast(e.message,true);unfocus();refresh()}}
function unfocus(){const a=document.activeElement;if(a&&(a.tagName==="SELECT"||a.tagName==="INPUT"))a.blur()}
function later(key,ch){clearTimeout(timers[key]);timers[key]=setTimeout(()=>save(ch()),300)}
$("mode").onchange=()=>save({mode:$("mode").value});
$("unit").onchange=()=>save({fahrenheit:$("unit").value==="f"});
$("cycle").oninput=()=>{if($("cycle").value!=="")later("c",()=>({cycle:parseFloat($("cycle").value)}))};
$("alarm-on").onchange=()=>{clearTimeout(timers.a);save({alarm:$("alarm-on").checked?(parseFloat($("alarm").value)||defAlarm):null})};
$("alarm").oninput=()=>{if($("alarm").value!=="")later("a",()=>({alarm:parseFloat($("alarm").value)}))};
$("fan").onchange=()=>{const v=$("fan").value,p=v===""?[null,null]:v.split("|");save({fan_hwmon:p[0],fan_input:p[1]})};
$("svc").onchange=async()=>{try{await api("/api/service",{action:$("svc").checked?"start":"stop"});toast("Aplicado")}catch(e){toast(e.message,true);unfocus()}refresh()};
$("test").onclick=async()=>{$("test").disabled=true;toast("Testando...");try{await api("/api/test",{});toast("Teste concluído")}catch(e){toast(e.message,true)}$("test").disabled=false};
$("log").onclick=async()=>{const p=$("logbox");try{p.textContent=(await api("/api/log")).log;p.hidden=false}catch(e){toast(e.message,true)}};
history.replaceState(null,"","/");
async function loop(){await refresh();setTimeout(loop,1000)}
loop();
</script></body></html>
"""


# --------------------------------------------------------------- server


def make_server(path, port=0, token=None):
    token = token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.settings_path, server.token = path, token
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = "chione-web"
    timeout = 10

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else (
            body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, msg):
        self._send(code, {"error": msg})

    def _cookie_name(self):
        return f"chione_{self.server.server_address[1]}"

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self._cookie_name():
                return value
        return ""

    def _host_ok(self):
        if host_allowed(self.headers.get("Host", ""),
                        self.server.server_address[1]):
            return True
        self._error(403, "host não permitido")
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.server.settings_path
        url = urlsplit(self.path)
        if url.path == "/":
            given = parse_qs(url.query).get("t", [""])[0]
            cookie = self._cookie()
            if token_ok(self.server.token, given):
                cookie_hdr = [("Set-Cookie", self._cookie_name() + "="
                               + self.server.token
                               + "; HttpOnly; SameSite=Strict; Path=/")]
            elif token_ok(self.server.token, cookie):
                cookie_hdr = []
            else:
                self._send(403, "Acesso negado. Use o endereço completo "
                           "mostrado no terminal onde o chione-web foi "
                           "iniciado.", "text/plain")
                return
            page = PAGE.replace("__TOKEN__", self.server.token)
            self._send(200, page, "text/html", cookie_hdr)
        elif not token_ok(self.server.token,
                          self.headers.get("X-Chione-Token", "")):
            self._error(403, "token inválido")
        elif self.path == "/api/state":
            self._send(200, api_state(path))
        elif self.path == "/api/log":
            self._send(200, {"log": g.journal_tail()})
        else:
            self._error(404, "não encontrado")

    def _body(self):
        """Parsed JSON body, or None after sending an error."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype != "application/json":
            self._error(415, "Content-Type deve ser application/json")
            return None
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "Content-Length ausente")
            return None
        if size < 0 or size > MAX_BODY:
            self._error(413, "corpo grande demais")
            return None
        try:
            return json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            self._error(400, "JSON inválido")
            return None

    def do_POST(self):
        if not self._host_ok():
            return
        if not token_ok(self.server.token,
                        self.headers.get("X-Chione-Token", "")):
            self._error(403, "token inválido")
            return
        if self.path not in ("/api/settings", "/api/service", "/api/test"):
            self._error(404, "não encontrado")
            return
        data = self._body()
        if data is None:
            return
        try:
            if self.path == "/api/settings":
                result = api_update(self.server.settings_path, data)
            elif self.path == "/api/service":
                if not isinstance(data, dict):
                    raise ValueError("esperado um objeto JSON")
                result = api_service(data.get("action"))
            else:
                with LOCK:
                    err = g.run_display_test()
                if err:
                    self._error(500, err)
                    return
                result = {"ok": True}
        except ValueError as exc:
            self._error(400, str(exc))
        except FileExistsError as exc:
            self._error(409, str(exc))
        except OSError as exc:
            self._error(500, str(exc))
        else:
            self._send(200, result)


def write_launcher(url):
    """0600 HTML file that redirects to url, so the token never shows up in
    the browser's command line. Returns its path."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        fd, path = tempfile.mkstemp(prefix="chione-web-", suffix=".html",
                                    dir=base)
    else:
        fd, path = tempfile.mkstemp(prefix="chione-web-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0;url={url}">'
                f'<script>location.replace({json.dumps(url)})</script>')
    return path


def open_browser(url, delay=30):
    try:
        path = write_launcher(url)
    except OSError:
        return None
    webbrowser.open("file://" + path)

    def cleanup():
        time.sleep(delay)
        remove_quiet(path)
    threading.Thread(target=cleanup, daemon=True).start()
    return path


def remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Painel web do Chione Display")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--settings", default=None, metavar="PATH")
    args = ap.parse_args(argv)
    path = args.settings or core.settings_path()
    try:
        server = make_server(path, args.port)
    except OSError as exc:
        print(f"Não foi possível abrir a porta {args.port}: {exc}",
              file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={server.token}"
    print(f"Chione Display: {url} (Ctrl+C para sair)", flush=True)
    launcher = []
    has_display = os.environ.get("DISPLAY") or os.environ.get(
        "WAYLAND_DISPLAY")
    if not args.no_browser and has_display:
        # The socket is already bound, so the browser can connect at once;
        # a terminal browser must not block the server.
        threading.Thread(
            target=lambda: launcher.append(open_browser(url)),
            daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for path in launcher:
            if path:
                remove_quiet(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
