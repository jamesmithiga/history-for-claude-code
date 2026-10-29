"""Generate a fictional Claude Code history for screenshots and demos.

Writes <out>/projects/<encoded-cwd>/<session>.jsonl in the same record format
Claude Code uses (see docs/history-format.md). Nothing here comes from a real
history. Usage:

    python tools/demo_history.py OUT_DIR [--root C:\\demo]

--root is where the fictional repositories "live"; create those folders if you
want the app to treat the sessions as resumable.
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)


def encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class Session:
    def __init__(self, cwd: str, branch: str, title: str, days_ago: float):
        self.cwd, self.branch, self.title = cwd, branch, title
        self.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo:{cwd}:{title}"))
        self.t = NOW - timedelta(days=days_ago)
        self.records: list[dict] = []
        self.n = 0

    def _base(self, kind: str) -> dict:
        self.n += 1
        self.t += timedelta(seconds=17 + 23 * (self.n % 5))
        return {"type": kind, "sessionId": self.id, "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.id}:{self.n}")),
                "timestamp": self.t.isoformat().replace("+00:00", "Z"), "cwd": self.cwd, "gitBranch": self.branch,
                "entrypoint": "claude-vscode", "version": "2.1.312", "isSidechain": False}

    def user(self, text: str) -> "Session":
        r = self._base("user")
        r["message"] = {"role": "user", "content": text}
        self.records.append(r)
        return self

    def claude(self, text: str) -> "Session":
        r = self._base("assistant")
        r["message"] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        self.records.append(r)
        return self

    def tool(self, name: str, inp: dict, output: str, error: bool = False) -> "Session":
        tid = f"toolu_{self.n:04d}"
        r = self._base("assistant")
        r["message"] = {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}
        self.records.append(r)
        r = self._base("user")
        r["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": output,
                                                      "is_error": error}]}
        self.records.append(r)
        return self

    def write(self, out: Path) -> None:
        d = out / "projects" / encode(self.cwd)
        d.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in self.records]
        lines.append(json.dumps({"type": "ai-title", "aiTitle": self.title, "sessionId": self.id}))
        (d / f"{self.id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(root: str) -> list[Session]:
    j = lambda *p: "\\".join([root.rstrip("\\"), *p])
    shop, pay, etl, mob = j("shopfront"), j("payments-api"), j("sales-pipeline"), j("mobile-app")
    s = []

    s.append(Session(pay, "feature/webhook-retries", "Stripe webhook retries and idempotency", 4.2)
        .user("Stripe is retrying webhooks and we're charging some customers twice. Logs show "
              "`duplicate key value violates unique constraint \"payments_idempotency_key\"` right before the double charge.")
        .tool("Read", {"file_path": f"{pay}\\app\\webhooks\\stripe.py"},
              "async def handle_event(event):\n    payment = await create_payment(event.data.object)\n"
              "    await charge(payment)   # runs again on every Stripe retry\n    return {\"ok\": True}")
        .claude("Found it. The handler charges *before* it records the event, so when Stripe retries after a timeout "
                "we insert the payment again, hit the unique constraint, and the retry path calls `charge()` a second time.\n\n"
                "Fix: record `event.id` in a `processed_events` table first, inside the same transaction, and return 200 "
                "early if it's already there. Then make outbound calls with an idempotency key and retry with exponential backoff.")
        .tool("Edit", {"file_path": f"{pay}\\app\\webhooks\\stripe.py", "old_string": "payment = await create_payment",
                       "new_string": "if await already_processed(event.id):\n        return {\"ok\": True}\n    payment = await create_payment"},
              "File updated")
        .tool("Bash", {"command": "pytest tests/webhooks -q", "description": "Run webhook tests"},
              "........                                                     [100%]\n8 passed in 1.92s")
        .claude("Decision: webhooks are idempotent on `event.id`, and charges use `Idempotency-Key: payment.id`. "
                "Retries use exponential backoff (1s, 2s, 4s, max 5 attempts) with jitter. All 8 webhook tests pass."))

    s.append(Session(pay, "main", "Alembic migration fails on production", 20.1)
        .user("The deploy failed: `sqlalchemy.exc.OperationalError: duplicate column name: owner_id` in migration 0042.")
        .tool("Bash", {"command": "alembic history | head -5", "description": "Show recent migrations"},
              "0042 -> 0043 (head), add invoices.owner_id\n0041 -> 0042, backfill owners\n0040 -> 0041, create owners")
        .claude("0042 was applied by hand on production last week, so the column already exists there. I'll make the "
                "migration idempotent: inspect the table and only add `owner_id` when it's missing.")
        .tool("Edit", {"file_path": f"{pay}\\migrations\\versions\\0042_backfill_owners.py",
                       "old_string": "op.add_column(", "new_string": "if 'owner_id' not in cols:\n        op.add_column("},
              "File updated")
        .claude("Decision: all migrations from now on check the inspector before adding or dropping columns, so they're "
                "safe to re-run after manual hotfixes."))

    s.append(Session(etl, "main", "Retry flaky S3 downloads in the nightly job", 27.4)
        .user("The nightly sales ETL fails about once a week with `botocore.exceptions.ReadTimeoutError` while downloading "
              "the regional CSVs. Can we make it retry?")
        .claude("Yes. botocore has built-in retries; the default 'legacy' mode only tries 4 times with a short backoff. "
                "Switching to 'adaptive' mode with more attempts covers these timeouts, and I'll add a retry with "
                "exponential backoff around our own download step for partial files.")
        .tool("Edit", {"file_path": f"{etl}\\etl\\s3.py", "old_string": "boto3.client(\"s3\")",
                       "new_string": "boto3.client(\"s3\", config=Config(retries={\"max_attempts\": 10, \"mode\": \"adaptive\"}))"},
              "File updated")
        .tool("Bash", {"command": "python -m etl.nightly --date 2026-10-01 --dry-run", "description": "Dry-run the nightly job"},
              "Downloaded 12/12 regions (2 retried)\nRows: 418,227 · Revenue: 3,912,044.17\nDry run OK"))

    s.append(Session(etl, "main", "Nightly sales ETL to Excel report", 12.6)
        .user("Finance wants the nightly totals as an Excel workbook with one sheet per region.")
        .tool("Bash", {"command": "python -c \"import openpyxl, pandas; print(openpyxl.__version__, pandas.__version__)\"",
                       "description": "Check library versions"}, "3.1.5 2.2.3")
        .claude("I'll write the report with `pandas.ExcelWriter(engine='openpyxl')`, one sheet per region, then reopen it "
                "with `openpyxl.load_workbook` to set number formats and freeze the header row.")
        .tool("Bash", {"command": "python -m etl.report --date 2026-10-16", "description": "Build the report"},
              "Wrote reports/sales-2026-10-16.xlsx (12 sheets, 418,227 rows)"))

    s.append(Session(shop, "main", "Users logged out every 15 minutes", 7.3)
        .user("Users are logged out every 15 minutes even though their refresh token should last 30 days.")
        .tool("Read", {"file_path": f"{shop}\\src\\auth\\refresh.ts"},
              "export async function refresh(req, res) {\n  const token = await rotate(req.cookies.rt)\n"
              "  res.json({ access: sign(token.user) })   // new refresh token never sent back\n}")
        .claude("The refresh handler rotates the refresh token on the server but never sets the new cookie, so the next "
                "refresh presents the old token, which has been revoked: `401 token_reused`. I'll set the rotated cookie "
                "with `HttpOnly; Secure; SameSite=Lax`.")
        .tool("Bash", {"command": "npm test -- auth", "description": "Run auth tests"},
              "PASS  src/auth/refresh.test.ts\nTests: 14 passed, 14 total"))

    s.append(Session(shop, "perf/checkout", "Speed up the checkout page", 15.8)
        .user("Checkout takes 2.4 s to become interactive on mid-range phones. Where's the time going?")
        .claude("The cart summary re-renders on every keystroke in the address form, and each render recalculates tax for "
                "every line item. Memoising the totals with `useMemo` and moving the address form state down a level cuts "
                "the work by about 80%.")
        .tool("Bash", {"command": "npx lighthouse http://localhost:3000/checkout --only-categories=performance --quiet",
                       "description": "Measure checkout performance"},
              "Performance: 94  ·  Time to Interactive: 0.9 s  ·  Total Blocking Time: 40 ms"))

    s.append(Session(mob, "main", "Push notification deep links", 31.0)
        .user("Tapping an order-shipped notification should open that order, but it always opens the home screen.")
        .claude("The notification payload uses `orderId`, but the linking config expects `/orders/:id`. I'll map the payload "
                "to a URL in the notification handler and register the route in the linking config, with a retry if the "
                "navigator isn't ready yet on a cold start.")
        .tool("Bash", {"command": "npx jest notifications", "description": "Run notification tests"},
              "Tests: 6 passed, 6 total"))

    s.append(Session(mob, "upgrade/rn-076", "Upgrade to React Native 0.76", 2.1)
        .user("Let's upgrade to React Native 0.76 and the new architecture.")
        .tool("Bash", {"command": "npx react-native upgrade 0.76.1", "description": "Run the upgrade helper"},
              "Upgraded react-native 0.74.5 -> 0.76.1\n3 files need manual merging: android/build.gradle, ios/Podfile, metro.config.js")
        .claude("Upgrade applied. Three native files need manual merges; I've resolved `metro.config.js`. Android and iOS "
                "build files are next, then I'll enable the new architecture flag and run the full test suite."))
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--root", default="C:\\demo")
    args = ap.parse_args()
    sessions = build(args.root)
    for sess in sessions:
        sess.write(args.out)
    print(f"Wrote {len(sessions)} demo sessions to {args.out / 'projects'}")


if __name__ == "__main__":
    main()
