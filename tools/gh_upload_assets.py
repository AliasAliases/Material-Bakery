"""Upload the built zips as GitHub Release assets.

Why a file and not `-c`: PowerShell eats quotes in command-line python (and
`> file` would write UTF-16). This script talks to the API with urllib, which
uses OpenSSL -- the same backend git uses successfully. curl.exe is NOT usable
here: it goes through schannel and the sandbox blocks it
(`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`).

Usage:
    python tools/gh_upload_assets.py --dry-run
    python tools/gh_upload_assets.py
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
TOKEN_FILE = os.path.join(WS, "DeepSeek Harness Git Token.txt")
API = "https://api.github.com"

PRIVATE = "AliasAliases/Material-Bakery-QuadRemesher"
PUBLIC = "AliasAliases/Material-Bakery"

# (repo, asset file name on disk, asset name in the release, tag)
WANTED = [
    (PRIVATE, os.path.join(WS, "MaterialBakery", "material_bakery_install.zip"),
     "material_bakery_install.zip", "v1.1"),
    (PRIVATE, os.path.join(WS, "MaterialBakery", "QuadRemesher_install.zip"),
     "quadremesher_install.zip", "v1.1"),
    (PUBLIC, os.path.join(WS, "MaterialBakery", "material_bakery_install.zip"),
     "material_bakery_install.zip", "v1.1"),
]


def token():
    with open(TOKEN_FILE, encoding="utf-8") as handle:
        return handle.read().strip()


def call(method, url, auth, data=None, content_type="application/json"):
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "token {}".format(auth))
    request.add_header("User-Agent", "material-bakery-release")
    request.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read()
            return response.status, json.loads(body.decode("utf-8")) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body


def releases(repo, auth):
    status, payload = call("GET", "{}/repos/{}/releases".format(API, repo), auth)
    if status != 200:
        raise SystemExit("GET releases {} failed: {} {}".format(repo, status, payload))
    return payload


def upload(repo, release_id, path, name, auth):
    size = os.path.getsize(path)
    print("  uploading {} ({} bytes) -> {}".format(name, size, repo))
    with open(path, "rb") as handle:
        data = handle.read()
    url = "https://uploads.github.com/repos/{}/releases/{}/assets?name={}".format(
        repo, release_id, urllib.parse.quote(name))
    status, payload = call("POST", url, auth, data=data,
                           content_type="application/zip")
    if status in (200, 201):
        print("    ok  {} -> {}".format(name, payload.get("browser_download_url")))
        return True
    print("    FAILED {}: {}".format(status, str(payload)[:400]))
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    auth = token()
    print("token: {}... ({} chars)".format(auth[:4], len(auth)))

    cache = {}
    for repo, path, name, tag in WANTED:
        # ⚠ 每个仓库的 releases 只查一次。第一版把 releases() 调用写在循环体里，
        #   同一个仓库被查了三遍 —— 多打的请求既慢又容易撞上速率限制。
        if repo not in cache:
            cache[repo] = releases(repo, auth)
        rel = next((item for item in cache[repo] if item.get("tag_name") == tag), None)
        if rel is None:
            print("{}: no release with tag {} -- existing tags: {}".format(
                repo, tag, [item.get("tag_name") for item in cache[repo]]))
            continue
        existing = {asset["name"]: asset for asset in rel.get("assets", [])}
        print("{} release {} (id={}): assets = {}".format(
            repo, tag, rel["id"], sorted(existing) or "none"))
        if not os.path.exists(path):
            print("  missing on disk: {}".format(path))
            continue
        old = existing.get(name)
        if old is not None:
            print("  asset {} already exists (id={}); deleting it first".format(
                name, old["id"]))
            if not args.dry_run:
                status, payload = call(
                    "DELETE",
                    "{}/repos/{}/releases/assets/{}".format(API, repo, old["id"]),
                    auth)
                print("    delete -> {}".format(status))
        if args.dry_run:
            print("  [dry-run] would upload {} from {}".format(name, path))
            continue
        upload(repo, rel["id"], path, name, auth)
    print("done")


if __name__ == "__main__":
    main()
