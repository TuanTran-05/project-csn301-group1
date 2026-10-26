import re


def test_static_urls_carry_a_content_hash_and_pages_are_not_cached(client, app, tmp_path):
    page = client.get("/projects")
    html = page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-cache"
    match = re.search(r'/static/js/projects\.js\?v=([0-9a-f]{12})"', html)
    assert match, "projects.js must be referenced with ?v=<hash>"
    # the versioned URL is served as the plain file
    asset = client.get(f"/static/js/projects.js?v={match.group(1)}")
    assert asset.status_code == 200 and b"pnetBootRun" in asset.data
    asset.close()


def test_the_version_follows_the_file_content(app, tmp_path):
    from flask import url_for

    app.static_folder = str(tmp_path)
    target = tmp_path / "x.js"
    target.write_text("one")
    with app.test_request_context():
        first = url_for("static", filename="x.js")
    target.write_text("two!")
    import os
    os.utime(target, (1, 1))  # a different mtime, whatever the clock says
    with app.test_request_context():
        second = url_for("static", filename="x.js")
        missing = url_for("static", filename="nope.js")
    assert first != second and "?v=" in first and "?v=" in second
    assert missing.endswith("/nope.js")
