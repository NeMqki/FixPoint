// FixPoint のデスクトップアプリ。
//
// 1. 起動アニメーション (splash/index.html) を表示する
// 2. FixPoint (同梱の Python + launcher.py) がまだ動いていなければ起動する
// 3. 準備ができたら、アニメーションを締めくくってから FixPoint の画面に切り替える
// 4. ウィンドウを閉じたらアプリは終わる。FixPoint 本体は画面がなくなったのを見て、
//    AI の作業が終わってから自分で終了する (--idle-exit)
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

const DEFAULT_PORT: u16 = 8765;
const START_TIMEOUT: Duration = Duration::from_secs(120); // 起動時の自動更新を待つこともある
const GONE_AFTER: Duration = Duration::from_secs(45); // 更新で再起動するあいだは待つ

/// FixPoint.exe が置いてあるフォルダ (= インストール先)
fn app_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_default()
}

fn arg_value(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

/// --port > config.json の port > 8765
fn port(dir: &Path, args: &[String]) -> u16 {
    if let Some(p) = arg_value(args, "--port").and_then(|v| v.parse().ok()) {
        return p;
    }
    std::fs::read_to_string(dir.join("config.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s.trim_start_matches('\u{feff}')).ok())
        .and_then(|v| v.get("port").and_then(|p| p.as_u64()))
        .and_then(|p| u16::try_from(p).ok())
        .filter(|p| *p != 0)
        .unwrap_or(DEFAULT_PORT)
}

/// そのポートで FixPoint が応答するか
fn is_fixpoint(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(800)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(3)));
    let req = format!("GET /api/info HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Fixpoint: 1\r\n\r\n");
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let _ = s.take(4 << 20).read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);
    let Some(body) = text.split_once("\r\n\r\n").map(|(_, b)| b) else {
        return false;
    };
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("app").and_then(|a| a.as_str()).map(|a| a == "fixpoint"))
        .unwrap_or(false)
}

/// 同梱の Python で launcher.py を起動する (自動更新もここで行われる)
fn start_server(dir: &Path, port: u16, args: &[String]) -> Result<(), String> {
    let launcher = dir.join("launcher.py");
    if !launcher.is_file() {
        return Err(format!("{} が見つかりません。FixPoint を入れ直してください。", launcher.display()));
    }
    let bundled = dir.join("python").join("pythonw.exe");
    let python = if bundled.is_file() { bundled } else { PathBuf::from("pythonw") };
    let mut cmd = Command::new(&python);
    cmd.arg(&launcher)
        .args(["--no-browser", "--idle-exit", "--port", &port.to_string()])
        .current_dir(std::env::var_os("USERPROFILE").map(PathBuf::from).unwrap_or_else(|| dir.to_path_buf()));
    if let Some(p) = arg_value(args, "--project") {
        cmd.args(["--project", &p]);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        cmd.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS);
    }
    cmd.spawn()
        .map(|_| ())
        .map_err(|e| format!("Python を起動できませんでした ({}): {e}", python.display()))
}

fn js_str(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}

fn is_splash(url: &Url) -> bool {
    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
}

/// FixPoint の外のリンクは、いつものブラウザで開く
fn open_external(url: &Url) {
    if matches!(url.scheme(), "http" | "https") {
        #[cfg(windows)]
        let _ = Command::new("rundll32").args(["url.dll,FileProtocolHandler", url.as_str()]).spawn();
    }
}

fn run(win: WebviewWindow, splash_ready: Arc<AtomicBool>, dir: PathBuf, port: u16, args: Vec<String>) {
    let fail = |msg: String| {
        while !splash_ready.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(50));
        }
        let _ = win.eval(&format!("window.fxFail && window.fxFail({})", js_str(&msg)));
    };

    if !is_fixpoint(port) {
        if let Err(e) = start_server(&dir, port, &args) {
            return fail(e);
        }
        let t0 = Instant::now();
        while !is_fixpoint(port) {
            if t0.elapsed() > START_TIMEOUT {
                let log = dir.join("fixpoint.log");
                return fail(format!("FixPoint を起動できませんでした。くわしくは {} を見てください。", log.display()));
            }
            thread::sleep(Duration::from_millis(250));
        }
    }

    while !splash_ready.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(50));
    }
    let url = format!("http://127.0.0.1:{port}/?app=1");
    let _ = win.eval(&format!("window.fxReady && window.fxReady({})", js_str(&url)));

    // 本体がいなくなったら (「終了」ボタン・アンインストールなど) アプリも閉じる
    let mut gone_since: Option<Instant> = None;
    loop {
        thread::sleep(Duration::from_secs(3));
        if is_fixpoint(port) {
            gone_since = None;
        } else if gone_since.get_or_insert_with(Instant::now).elapsed() > GONE_AFTER {
            win.app_handle().exit(0);
            return;
        }
    }
}

fn focus(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    tauri::Builder::default()
        // 2回目に起動されたら、新しく開かずに今のウィンドウを前に出す
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| focus(app)))
        .setup(move |app| {
            let dir = app_dir();
            let port = port(&dir, &args);
            let splash_ready = Arc::new(AtomicBool::new(false));
            let handle = app.handle().clone();
            let loaded = splash_ready.clone();

            let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("FixPoint")
                .inner_size(1400.0, 900.0)
                .min_inner_size(880.0, 560.0)
                .center()
                .visible(false) // 白い画面がちらつかないよう、アニメーションの準備ができてから見せる
                .data_directory(dir.join("webview"))
                .on_page_load(move |w, p| {
                    if p.event() == PageLoadEvent::Finished {
                        let _ = w.show();
                        let _ = w.set_focus();
                        if is_splash(p.url()) {
                            loaded.store(true, Ordering::SeqCst);
                        }
                    }
                })
                .on_navigation(move |url| {
                    if is_splash(url) {
                        return true;
                    }
                    if url.host_str() == Some("127.0.0.1") && url.port() == Some(port) {
                        if url.path() == "/__quit" {
                            handle.exit(0); // 画面の「終了」ボタン
                            return false;
                        }
                        return true;
                    }
                    open_external(url);
                    false
                })
                .build()?;

            let args = args.clone();
            thread::spawn(move || run(win, splash_ready, dir, port, args));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("FixPoint を起動できませんでした");
}
