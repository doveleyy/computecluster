import argparse
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(
        description="Verify a running Phase 2 fixture through the core gateway"
    )
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    trusted = {"X-Forwarded-Proto": "https", "Tailscale-User-Login": args.login}

    def exchange(path, headers):
        outgoing = Request(args.core_url + path, headers=headers)
        try:
            response = opener.open(outgoing, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    status, _, body = exchange(args.route + "/", trusted)
    assert status == 200 and f'data-user-id="{args.user_id}"'.encode() in body
    status, _, _ = exchange(args.route + "/", {"Tailscale-User-Login": args.login})
    assert status == 401
    status, _, body = exchange(
        args.route + "/echo",
        {
            **trusted,
            "X-Platform-User-Context": "forged",
            "X-API-Token": "forged",
            "Cookie": "home_platform_fake=forged; fixture_session=okay",
        },
    )
    echo = json.loads(body)
    assert (
        status == 200
        and echo["context_count"] == 1
        and not echo["platform_cookie_present"]
        and echo["app_cookie_present"]
    )
    for case in ("reserved", "outside", "domain", "pathless"):
        status, _, _ = exchange(args.route + "/cookie?case=" + case, trusted)
        assert status == 502, case
    status, headers, _ = exchange(args.route + "/cookie?case=inside", trusted)
    assert status == 200 and "Path=" + args.route in headers["Set-Cookie"]
    print(
        json.dumps(
            {
                "stable_user": "passed",
                "forged_http": "refused",
                "request_boundaries": "passed",
                "cookie_boundaries": "passed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
