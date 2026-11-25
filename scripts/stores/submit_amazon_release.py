#!/usr/bin/env python3
"""Submit a verified Fire TV APK to the existing Amazon Appstore listing."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode

STORE = "developer.amazon.com"
LARGE_APK_BYTES = 300_000_000


class AmazonError(RuntimeError):
    def __init__(self, operation, status, result):
        self.status = status
        errors = result.get("errors") if isinstance(result, dict) else None
        codes = [item.get("errorCode") for item in errors or [] if isinstance(item, dict)]
        codes = [code for code in codes if isinstance(code, str) and re.fullmatch(r"[\w.-]{1,100}", code)]
        super().__init__(f"Amazon {operation} returned HTTP {status}" + (f" ({', '.join(codes)})" if codes else ""))


def request(host, method, path, *, token=None, payload=None, apk=None, etag=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-Match"] = etag
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif method == "POST" and apk is None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection = http.client.HTTPSConnection(host, timeout=600)
    try:
        if apk is None:
            connection.request(method, path, body=body, headers=headers)
        else:
            headers.update({"Content-Type": "application/octet-stream", "fileName": apk.name})
            with apk.open("rb") as stream:
                headers["Content-Length"] = str(os.fstat(stream.fileno()).st_size)
                connection.request(method, path, body=stream, headers=headers)
        response = connection.getresponse()
        encoded = response.read(2_000_001)
        if len(encoded) > 2_000_000:
            raise RuntimeError("Amazon response exceeded the expected size")
        try:
            result = json.loads(encoded) if encoded else None
        except (ValueError, UnicodeError):
            if 200 <= response.status < 300:
                raise RuntimeError("Amazon returned an invalid JSON response") from None
            result = None
        return response.status, result, response.getheader("ETag")
    finally:
        connection.close()


def require_etag(etag):
    if not etag:
        raise RuntimeError("Amazon did not return the required ETag")
    return etag


class Appstore:
    def __init__(self, app_id, client_id, client_secret):
        if not re.fullmatch(r"amzn1\.devportal\.mobileapp\.[0-9a-f]{32}", app_id):
            raise ValueError("Configure the Amazon Developer Console App ID")
        if not client_id or not client_secret:
            raise ValueError("Configure the Amazon Appstore API credentials")
        self.path = f"/api/appstore/v1/applications/{app_id}/edits"
        form = urlencode({"grant_type": "client_credentials", "scope": "appstore::apps:readwrite",
                          "client_id": client_id, "client_secret": client_secret})
        connection = http.client.HTTPSConnection("api.amazon.com", timeout=30)
        try:
            connection.request("POST", "/auth/o2/token", body=form, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise AmazonError("authentication", response.status, None)
            self.token = json.loads(response.read(128_000))["access_token"]
        finally:
            connection.close()

    def call(self, operation, method, suffix="", *, payload=None, apk=None, etag=None):
        status, result, tag = request(
            STORE, method, self.path + suffix, token=self.token, payload=payload, apk=apk,
            etag=etag,
        )
        if not 200 <= status < 300:
            raise AmazonError(operation, status, result)
        return result, tag


def resource_path(resource):
    identifier = resource["id"]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Amazon did not return a valid resource ID")
    return "/" + quote(identifier, safe="")


def targeting_choices(targeting):
    return (
        targeting.get("otherAndroidDevices"),
        *(sorted((device["id"], device["status"]) for device in targeting.get(group, []))
          for group in ("amazonDevices", "nonAmazonDevices")),
    )


def replace_binary(store, edit, old, apk, code):
    binary = edit + "/apks" + resource_path(old)
    if apk.stat().st_size <= LARGE_APK_BYTES:
        _, etag = store.call("read APK", "GET", binary)
        updated, _ = store.call("replace APK", "PUT", binary + "/replace", apk=apk, etag=require_etag(etag))
    else:
        targeting, _ = store.call("read existing targeting", "GET", binary + "/targeting")
        uploaded, _ = store.call("upload large APK", "POST", edit + "/apks/large/upload", apk=apk)
        file_id = uploaded["fileId"]
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("Amazon large upload did not return a file ID")
        updated, _ = store.call("attach large APK", "POST", edit + "/apks/attach", payload={"fileId": file_id})
        if updated["versionCode"] != code or updated["id"] == old["id"]:
            raise ValueError("Amazon did not attach the expected new APK")
        _, etag = store.call("read replaced APK", "GET", binary)
        store.call("remove superseded APK from draft", "DELETE", binary, etag=require_etag(etag))
        new_path = edit + "/apks" + resource_path(updated) + "/targeting"
        current, etag = store.call("read new targeting", "GET", new_path)
        if targeting_choices(current) != targeting_choices(targeting):
            store.call("copy device targeting", "PUT", new_path, payload=targeting, etag=require_etag(etag))
        current, _ = store.call("verify device targeting", "GET", new_path)
        if targeting_choices(current) != targeting_choices(targeting):
            raise ValueError("Amazon did not preserve the existing APK device targeting")
    if updated["versionCode"] != code:
        raise ValueError("Amazon reported an unexpected replacement APK version code")


def fill_required_release_notes(store, edit, tag):
    listings_path = edit + "/listings"
    result, _ = store.call("list store locales", "GET", listings_path)
    for language, listing in result["listings"].items():
        if (listing.get("recentChanges") or "").strip():
            continue
        path = listings_path + "/" + quote(language, safe="")
        current, etag = store.call("read localized listing", "GET", path)
        if (current.get("recentChanges") or "").strip():
            continue
        current["recentChanges"] = (
            f"Pleazyng {tag[1:]} for Fire TV. Full release notes are available in the Pleazyng project on GitHub."
        )
        store.call("set required release notes", "PUT", path, payload=current, etag=require_etag(etag))


def submit(store, apk, code, tag, receipt):
    active, _ = store.call("read open edit", "GET")
    if active and active.get("id"):
        raise RuntimeError("Amazon has an open edit. Automatic delivery will wait for it to be resolved.")
    created, _ = store.call("create edit", "POST")
    edit = resource_path(created)
    receipt["edit_id"] = created["id"]
    commit_attempted = False
    try:
        existing, _ = store.call("list copied APKs", "GET", edit + "/apks")
        if not isinstance(existing, list) or len(existing) != 1:
            raise RuntimeError("Automatic delivery requires exactly one existing Amazon APK")
        old = existing[0]
        current_code = old["versionCode"]
        if type(current_code) is not int or current_code < 1:
            raise ValueError("Amazon returned an invalid current APK version code")
        receipt["previous_version_code"] = current_code
        if current_code < code:
            replace_binary(store, edit, old, apk, code)
            versions, _ = store.call("verify draft APK", "GET", edit + "/apks")
            if [item["versionCode"] for item in versions] != [code]:
                raise RuntimeError("The Amazon draft does not contain exactly the expected APK")
            fill_required_release_notes(store, edit, tag)
            _, etag = store.call("read draft before validation", "GET", edit)
            _, etag = store.call("validate draft", "POST", edit + "/validate", etag=require_etag(etag))
            if not etag:
                _, etag = store.call("read validated draft", "GET", edit)
            commit_etag = require_etag(etag)
            commit_attempted = True
            receipt["commit_attempted"] = True
            store.call("submit for review", "POST", edit + "/commit", etag=commit_etag)
            return "submitted_for_review"
        return "already_current_or_newer"
    finally:
        if not commit_attempted:
            try:
                _, etag = store.call("read task draft before cleanup", "GET", edit)
                store.call("remove task draft", "DELETE", edit, etag=require_etag(etag))
            except (AmazonError, RuntimeError, OSError, http.client.HTTPException):
                receipt["draft_cleanup_required"] = True
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--version-code", type=int)
    parser.add_argument("--tag")
    parser.add_argument("--sha256")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if not args.probe and (not args.apk or not args.apk.is_file() or not args.version_code
                           or args.version_code < 1 or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag or "")
                           or not args.sha256
                           or not re.fullmatch(r"[0-9a-f]{64}", args.sha256)):
        parser.error("submission requires a verified APK, release tag, version code, and SHA256")
    receipt = {"state": "failed", "release_tag": args.tag, "apk_sha256": args.sha256,
               "version_code": args.version_code}
    try:
        store = Appstore(os.environ.get("AMAZON_APPSTORE_APP_ID", ""),
                         os.environ.get("AMAZON_APPSTORE_CLIENT_ID", ""),
                         os.environ.get("AMAZON_APPSTORE_CLIENT_SECRET", ""))
        if args.probe:
            active, _ = store.call("read open edit", "GET")
            receipt["state"] = "authenticated"
            receipt["open_edit"] = bool(active and active.get("id"))
        else:
            receipt["state"] = submit(store, args.apk, args.version_code, args.tag, receipt)
        print(f"Amazon delivery state: {receipt['state']}")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, http.client.HTTPException) as error:
        receipt["error"] = str(error)
        print(receipt["error"], file=sys.stderr)
        return 1
    finally:
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
