"""Upload the built zips as GitHub Release assets.

Why a file and not `-c`: PowerShell eats quotes in command-line python (and
`> file` would write UTF-16). This script talks to the API with urllib, which
uses OpenSSL -- the same backend git uses successfully. curl.exe is NOT usable
here: it goes through schannel and the sandbox blocks it
(`schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`).

Release asset naming rule (user, 2026-09-25: "私有库就叫
MaterialBakery-QuadRemesher-<tag>.zip，公有库就叫 material_bakery_install.zip"):

    private repo : MaterialBakery-QuadRemesher-<tag>.zip   <- version stamped
    public  repo : material_bakery_install.zip             <- stable name
    both repos   : quadremesher_install.zip   (the 40 MB QR payload)

So you only ever pass a TAG; the names are derived. Hard-coding them by hand is
exactly how the private release ended up with a stale alias next to a fresh
build (the alias said build 2026-09-23 while the canonical name said 2026-09-25).

Usage:
    python tools/gh_upload_assets.py --tag v1.1 --dry-run
    python tools/gh_upload_assets.py --tag v1.1
    python tools/gh_upload_assets.py --tag v1.1 --extra-alias   # also keep the
        canonical material_bakery_install.zip on the private release
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

INSTALL_ZIP = os.path.join(WS, "MaterialBakery", "material_bakery_install.zip")
QR_ZIP = os.path.join(WS, "MaterialBakery", "quadremesher_install.zip")


def wanted(tag, extra_alias=False):
    """[(repo, local path, asset name)] for this tag."""
    rows = [
        (PRIVATE, INSTALL_ZIP, "MaterialBakery-QuadRemesher-{}.zip".format(tag)),
        (PUBLIC, INSTALL_ZIP, "material_bakery_install.zip"),
    ]
    if os.path.exists(QR_ZIP):
        rows.append((PRIVATE, QR_ZIP, "quadremesher_install.zip"))
    if extra_alias:
        # The alias started life as a rename of the install zip, so keeping both
        # on the same release means two download links with identical bytes.
        rows.append((PRIVATE, INSTALL_ZIP, "material_bakery_install.zip"))
    return rows


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
    parser.add_argument("--tag", default="v1.1",
                        help="release tag to update (asset names are derived from it)")
    parser.add_argument("--extra-alias", action="store_true",
                        help="also upload material_bakery_install.zip on the "
                             "private release (same bytes as the versioned name)")
    args = parser.parse_args()

    auth = token()
    print("token: {}... ({} chars)".format(auth[:4], len(auth)))
    print("tag: {}".format(args.tag))
    for _repo, _path, name in wanted(args.tag, args.extra_alias):
        print("  will ensure: {}  <- {}".format(name, os.path.basename(_path)))

    cache = {}
    for repo, path, name in wanted(args.tag, args.extra_alias):
        # ⚠ 每个仓库的 releases 只查一次。第一版把 releases() 调用写在循环体里，
        #   同一个仓库被查了三遍 —— 多打的请求既慢又容易撞上速率限制。
        if repo not in cache:
            cache[repo] = releases(repo, auth)
        rel = next((item for item in cache[repo]
                    if item.get("tag_name") == args.tag), None)
        if rel is None:
            print("{}: no release with tag {} -- existing tags: {}".format(
                repo, args.tag, [item.get("tag_name") for item in cache[repo]]))
            continue
        existing = {asset["name"]: asset for asset in rel.get("assets", [])}
        print("{} release {} (id={}): assets = {}".format(
            repo, args.tag, rel["id"], sorted(existing) or "none"))
        if not os.path.exists(path):
            print("  missing on disk: {}".format(path))
            continue
        old = existing.get(name)
        if old is not None:
            print("  asset {} already exists ({} bytes, id={}); deleting it first".format(
                name, old.get("size"), old["id"]))
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
