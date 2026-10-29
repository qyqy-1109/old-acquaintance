"""Check that the backend actually serves all page resources."""
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi.testclient import TestClient
from app.main import app


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "img") and attrs.get("src"):
            self.resources.add(attrs["src"])
        if tag == "link" and attrs.get("href"):
            self.resources.add(attrs["href"])


def test_frontend_resources_are_served():
    client = TestClient(app)  # No lifespan: resource checks must not open real user databases.
    try:
        page = client.get("/")
        assert page.status_code == 200
        assert "no-store" in page.headers["cache-control"]
        parsed = PageResources()
        parsed.feed(page.text)
        assert parsed.resources, "the page must load its scripts, styles and images"
        for url in parsed.resources:
            assert url.startswith("/static/"), f"unexpected external resource: {url}"
            response = client.get(url)
            assert response.status_code == 200, f"{url}: {response.status_code}"
            assert response.content, f"empty resource: {url}"
    finally:
        client.close()


def test_life_board_puts_her_reports_first():
    """`#145`：看板以「她最近报备的」打头，「此刻」降到最后并标明只当氛围。

    为什么值得钉住顺序：用户报过两次"两处对不上"（对话里"汤炖好了"，状态页还写着
    "回家炖汤中"）——根子上是界面上"她说过的话"与"系统推演的此刻"**并列**、看不出主次。
    方向定成"虚拟 AI 女友"之后，**她亲口说的**才算准的。
    """
    client = TestClient(app)
    try:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        said_at = html.find('id="life-said"')
        state_at = html.find('id="life-state"')
        assert said_at > 0 and state_at > 0, "两个区块都要在页面上"
        assert said_at < state_at, "「她最近报备的」必须排在「此刻」前面"
        assert "她最近报备的" in html
        assert "只当氛围" in html
    finally:
        client.close()


def test_life_pin_dialog_contract_is_present():
    """T96：钉牵挂前必须能编辑报备，并保留 60 字边界。"""
    client = TestClient(app)
    try:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        for marker in (
            'id="life-pin-dialog"',
            'id="life-pin-form"',
            'id="life-pin-input"',
            'id="life-pin-error"',
            'id="life-pin-submit"',
        ):
            assert marker in html, f"钉牵挂编辑入口缺少 {marker}"
        assert 'id="life-pin-input"' in html and 'maxlength="60"' in html
    finally:
        client.close()


def test_relationship_board_contract_is_present():
    """关系看板必须有阶段、承诺、历史和新增承诺入口。"""
    client = TestClient(app)
    try:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        for marker in (
            'data-view="relationship"',
            'id="view-relationship"',
            'id="relationship-stage"',
            'id="relationship-call"',
            'id="relationship-promises"',
            'id="relationship-events"',
            'id="relationship-form"',
            'id="relationship-input"',
        ):
            assert marker in html, f"关系看板缺少 {marker}"
    finally:
        client.close()


def test_legacy_claim_dialog_contract_is_present():
    """T65：登录后旧数据认领必须有明确的说明、确认和暂不处理入口。"""
    client = TestClient(app)
    try:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        for marker in (
            'id="legacy-claim-dialog"',
            'id="legacy-claim-confirm"',
            'id="legacy-claim-dismiss"',
            'id="legacy-claim-message"',
        ):
            assert marker in html, f"旧数据认领缺少 {marker}"
    finally:
        client.close()


if __name__ == "__main__":
    test_frontend_resources_are_served()
    test_life_board_puts_her_reports_first()
    test_life_pin_dialog_contract_is_present()
    test_relationship_board_contract_is_present()
    test_legacy_claim_dialog_contract_is_present()
    print("frontend resources: all checks passed")
