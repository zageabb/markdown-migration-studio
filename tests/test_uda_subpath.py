"""UDA proxy prefix and direct LAN smoke tests."""
from app import app

def test_uda_and_lan_render():
    client=app.test_client()
    local=client.get("/")
    assert local.status_code==200
    assert '<base href="/">' in local.get_data(as_text=True)
    headers={
        "X-Forwarded-Prefix":"/apps/markdown-migration-studio",
        "X-Forwarded-Host":"tanyaanne.ddns.net",
        "X-Forwarded-Proto":"https",
    }
    proxied=client.get("/",headers=headers)
    assert proxied.status_code==200
    html=proxied.get_data(as_text=True)
    assert '<base href="/apps/markdown-migration-studio/">' in html
    assert '/apps/markdown-migration-studio/static/app.js' in html
    assert '/apps/markdown-migration-studio/static/app.css' in html
