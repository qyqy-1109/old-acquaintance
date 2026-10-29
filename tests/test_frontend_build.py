"""前端构建守卫：页面用到的类名、图标、源码登记，编译产物里到底对不对得上。

为什么需要这个测试：前端是 Tailwind —— **类名写在 HTML/JS 里，样式来自编译产物**
（`app/views/assets/styles.css`，由 `npm run build:frontend` 生成）。改了类名却忘了
重新编译时，页面不会报错，只是那条样式悄悄没了（本仓最忌的"静默失灵"）。
`test_frontend_contract.py` 只查"资源取不取得到"，查不出这种漂移。

零依赖的纯文件检查（不需要 Node、不需要浏览器）：

1. **类名漂移**：`index.html` 的 class 属性 + `app/views/js/*.js` 里动态创建的类名，
   逐个到编译后的 CSS 里找（按 CSS 转义规则）；
2. **图标管线**：`data-icon="x"` 必须有 `assets/icons/x.svg`，且编译 CSS 里有对应规则
   （否则图标变成空气，页面同样不报错）；
3. **源码登记**：输入 CSS 用的是 `@import "tailwindcss" source(none)` + 显式 `@source`，
   所以**新加的 JS/HTML 文件若没登记进 `@source`，里面的类名不会被编译**——这里守住它；
4. **没有 CDN**：页面源码里不许出现外部 http(s) 资源（离线/内网部署会白屏）。

改动前端后请先跑 `npm run build:frontend`，再跑这个测试。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
VIEWS = ROOT / "app" / "views"
HTML = VIEWS / "index.html"
JS_DIR = VIEWS / "js"
CSS_INPUT = VIEWS / "css" / "styles.css"
COMPILED_CSS = VIEWS / "assets" / "styles.css"
ICON_DIR = VIEWS / "assets" / "icons"

# 提取到的、但不是类名的字符串（属性名/路径/标签…），避免误报
NOISE = {
    "application/json", "text/plain", "text/html", "no-store", "percent-encoded", "bearer",
    "auth_token", "user_id", "auth_username", "utf-8", "content-type", "authorization",
    "x-user-id", "x-user-id-encoding", "domcontentloaded", "mouseover", "hover", "abort",
    "aborterror", "textcontent", "readystatechange",
}


def css_escape(token: str) -> str:
    """Tailwind 在 CSS 里会转义这些字符（`.lg\\:grid-cols-2`）。

    ⚠ 转义字符表里**不能包含反斜杠本身**：否则 `:` 先变 `\\:`，紧接着 `\\` 又被转义成
    `\\\\:`，所有带变体前缀的类都会被误报成"缺失"（这个坑真踩过，见台账 `#83`）。
    """
    for ch in ":.%/()#,[]!+*~'\"$&@^`{}|<>=?;":
        token = token.replace(ch, "\\" + ch)
    return token


def _sources() -> list[tuple[str, str]]:
    files = [HTML] + sorted(JS_DIR.glob("*.js"))
    return [(str(p.relative_to(ROOT)).replace("\\", "/"), p.read_text(encoding="utf-8"))
            for p in files if p.exists()]


def used_classes() -> dict[str, set[str]]:
    """{类名: 用到它的文件}。"""
    found: dict[str, set[str]] = {}
    for name, text in _sources():
        tokens: set[str] = set()
        for attr in re.finditer(r'class="([^"]+)"', text):
            tokens.update(attr.group(1).split())
        for helper in re.finditer(r'element\(\s*"[a-z0-9]+"\s*,\s*"([^"]+)"', text):
            tokens.update(helper.group(1).split())
        for call in re.finditer(r'classList\.(?:add|toggle|remove)\(\s*"([^"]+)"', text):
            tokens.update(call.group(1).split())
        for assign in re.finditer(r'className\s*=\s*"([^"]+)"', text):
            tokens.update(assign.group(1).split())
        for token in tokens - NOISE:
            if re.fullmatch(r"[a-z][a-z0-9:/\[\]().%,#!_-]*", token):
                found.setdefault(token, set()).add(name)
    return found


def used_icons() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, text in _sources():
        for match in re.finditer(r'data-icon="([a-z0-9-]+)"', text):
            found.setdefault(match.group(1), set()).add(name)
        for match in re.finditer(r'icon\(\s*"([a-z0-9-]+)"', text):
            found.setdefault(match.group(1), set()).add(name)
    return found


def selector_hooks() -> set[str]:
    """只被 JS **当选择器找**的类名（`.memory-section` 这类行为钩子，不需要 CSS）。

    这个区分是踩出来的：`memory-section` 在样式表里根本不存在，看着像死类名，
    但 `app.js` 用 `querySelectorAll(".memory-section")` 切换记忆页——删掉它，
    "世界书/状态/时间轴/画像/体检"就永远不显示了（而页面不会报错）。
    """
    hooks: set[str] = set()
    for _, text in _sources():
        for match in re.finditer(r'querySelector(?:All)?\(\s*"\.([a-z0-9_-]+)', text):
            hooks.add(match.group(1))
        for match in re.finditer(r'closest\(\s*"\.([a-z0-9_-]+)', text):
            hooks.add(match.group(1))
        for match in re.finditer(r'matches\(\s*"\.([a-z0-9_-]+)', text):
            hooks.add(match.group(1))
    return hooks


def dynamic_classes() -> set[str]:
    """JS 自己加上去的类（状态类，如 `open`／`offline`）——它们不需要预先写在页面里。"""
    dynamic: set[str] = set()
    for _, text in _sources():
        for match in re.finditer(r'classList\.(?:add|toggle|remove|contains)\(\s*"([^"]+)"', text):
            dynamic.update(match.group(1).split())
        for match in re.finditer(r'element\(\s*"[a-z0-9]+"\s*,\s*"([^"]+)"', text):
            dynamic.update(match.group(1).split())
    return dynamic


def js_hooks() -> set[str]:
    """JS 会碰到的全部类名（选择器 + 动态添加），用于"样式漂移"检查时豁免。"""
    return selector_hooks() | dynamic_classes()


def registered_sources() -> list[str]:
    """输入 CSS 里显式登记的源码路径（相对 app/views/）。"""
    return [m.group(1).strip().strip('"\'') for m in
            re.finditer(r'@source\s+("[^"]+"|\'[^\']+\'|[^\s;]+)', CSS_INPUT.read_text(encoding="utf-8"))]


def test_no_class_drift():
    css = COMPILED_CSS.read_text(encoding="utf-8")
    assert "tailwindcss" in css[:200], "compiled CSS looks like a placeholder, run npm run build:frontend"
    assert len(css) > 10000, f"compiled CSS is suspiciously small ({len(css)} bytes)"

    hooks = js_hooks()
    missing = {token: sorted(where) for token, where in used_classes().items()
               if token not in hooks and css_escape(token) not in css}
    assert not missing, (
        "these classes are used but missing from the compiled CSS — "
        "run `npm run build:frontend` and commit the artifact: "
        + ", ".join(f"{token} ({', '.join(where)})" for token, where in sorted(missing.items()))
    )


def test_js_hooks_exist_in_markup():
    """反向守卫：JS 用选择器找的类，页面里必须真的有——否则是静默失效。

    `memory-section` 就是这么被误判过一次：样式表里没有它，看着像死类名，
    删掉之后记忆页再也显示不出来（页面不报错）。所以两个方向都守。
    注意只查**选择器钩子**：`open`／`offline` 这类状态类是 JS 自己加上去的，不在此列。
    """
    html = HTML.read_text(encoding="utf-8")
    present: set[str] = set()
    for match in re.finditer(r'class="([^"]+)"', html):
        present.update(match.group(1).split())
    dynamic = dynamic_classes()
    orphans = sorted(h for h in selector_hooks() if h not in present and h not in dynamic)
    assert not orphans, (
        "these classes are used as JS selectors but exist neither in index.html nor in "
        f"JS-created markup — the behavior they drive is silently dead: {orphans}"
    )


def test_icons_are_complete():
    css = COMPILED_CSS.read_text(encoding="utf-8")
    icons = used_icons()
    assert icons, "no data-icon usage found — the icon pipeline check would be vacuous"
    # 压缩后的产物会把 `[data-icon="house"]` 写成 `[data-icon=house]`，两种形态都要认
    def has_rule(name: str) -> bool:
        return re.search(rf'\[data-icon=["\']?{re.escape(name)}["\']?\]', css) is not None

    missing_files = sorted(n for n in icons if not (ICON_DIR / f"{n}.svg").exists())
    missing_rules = sorted(n for n in icons if not has_rule(n))
    assert not missing_files, f"icon svg missing from assets/icons: {missing_files}"
    assert not missing_rules, f"icon rules missing from compiled CSS: {missing_rules}"


def test_sources_are_registered():
    """新加的前端文件必须登记进输入 CSS 的 @source，否则类名静默不编译。"""
    registered = registered_sources()
    assert registered, f"@{'source'} directives look missing from {CSS_INPUT.name}"
    unscanned = []
    for path, _ in _sources():
        # @source 写的是相对 app/views/ 的路径（如 ../index.html、../js）
        relative = path.replace("app/views/", "")
        if not any(relative == r.lstrip("./") or relative.startswith(r.lstrip("./").rstrip("/") + "/")
                   for r in registered):
            unscanned.append((path, registered))
    assert not unscanned, (
        "these frontend files are not covered by any @source directive, so their classes are "
        f"never compiled: {[p for p, _ in unscanned]} (registered: {registered})"
    )


def test_no_external_resources():
    external = []
    for name, text in _sources():
        for match in re.finditer(r'(?:src|href)\s*=\s*"(?:https?:)?//[^"]+"', text):
            external.append(f"{name}: {match.group(0)}")
        for match in re.finditer(r'url\(\s*["\']?(?:https?:)?//[^)"\']+', text):
            external.append(f"{name}: {match.group(0)}")
    for match in re.finditer(r'url\(\s*["\']?(?:https?:)?//[^)"\']+', COMPILED_CSS.read_text(encoding="utf-8")):
        external.append(f"assets/styles.css: {match.group(0)}")
    assert not external, "外部资源会让离线/内网部署白屏（本仓要求零 CDN）: " + "; ".join(external)


if __name__ == "__main__":
    test_no_class_drift()
    test_js_hooks_exist_in_markup()
    test_icons_are_complete()
    test_sources_are_registered()
    test_no_external_resources()
    print(f"frontend build: all checks passed ({len(used_classes())} classes, "
          f"{len(js_hooks())} js hooks, {len(used_icons())} icons, "
          f"{len(registered_sources())} registered sources, no external resources)")
