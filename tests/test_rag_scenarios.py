"""
Integration tests for the RAG pipeline.

Requires HTTP_DEBUG_ENDPOINTS_ENABLED=1 on the signal-bot.

Run:
  BOT_URL=http://161.33.64.115:8000 GROUP_ID=<id> python tests/test_rag_scenarios.py
  BOT_URL=http://161.33.64.115:8000 GROUP_ID=<id> pytest tests/test_rag_scenarios.py -v
"""
import os, sys, json
import requests

BOT_URL = os.getenv("BOT_URL", "http://161.33.64.115:8000")
GROUP_ID = os.getenv("GROUP_ID", "1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok=")
BASE = BOT_URL.rstrip("/")


def answer(question: str) -> dict:
    r = requests.post(
        f"{BASE}/debug/answer",
        json={"group_id": GROUP_ID, "question": question},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()


def retrieve(query: str) -> list:
    r = requests.post(
        f"{BASE}/retrieve",
        json={"group_id": GROUP_ID, "query": query, "k": 3},
        timeout=15,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json().get("cases", [])


class TestRAGScenarios:
    """Core scenarios that must pass."""

    def test_a_known_case_returns_link(self):
        """(a) Query matching a solved case → response contains a case link."""
        q = "як розпакувати .stabx файл або отримати старий zip формат зі стрімом у records?"
        res = answer(q)
        assert res["scrag_hits"] > 0, f"SCRAG found no cases! {res}"
        assert res["has_case_link"], \
            f"Expected case link in response. Response: {res['response']!r}"

    def test_a2_exact_user_question(self):
        """(a2) Exact question from the user screenshot → must have case link."""
        q = ("вітаю, раніше(2міс тому) в records завантажити давало зіп файл зі стрімом "
             "а зараз .stabx, як отримати попередній варіант або розпакувати .stabx?")
        res = answer(q)
        assert res["scrag_hits"] > 0, f"SCRAG found no cases! {res}"
        assert res["has_case_link"], \
            f"Expected case link for stabx question. Response: {res['response']!r}"

    def test_b_casual_chat_no_case_link(self):
        """(b) Casual chat → distance threshold filters cases → no case link sent.
        Note: In the live bot, gating LLM prevents these from reaching synthesis at all.
        Here we verify the distance threshold acts as a safety net."""
        for q in ["Всім привіт!", "Дякую 👍"]:
            res = answer(q)
            assert not res["has_case_link"], \
                f"Bot gave a case link for casual chat {q!r}: {res['response']!r}"

    def test_c_unknown_question_tags_admin(self):
        """(c) Support question with no known answer → admin tagged, no case link."""
        q = "як налаштувати VPN у мережі підприємства через Cisco ASA?"
        res = answer(q)
        assert res["is_admin_tag"], \
            f"Expected admin tag for unknown question. Response: {res['response']!r}"
        assert not res["has_case_link"], \
            f"Should not have a case link for unrelated question. Response: {res['response']!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("(a) .stabx exact question",
     "вітаю, раніше(2міс тому) в records завантажити давало зіп файл зі стрімом а зараз .stabx, як отримати попередній варіант або розпакувати .stabx?",
     True),
    ("(a) GPS drone",
     "дрон не бачить GPS, як увімкнути?",
     True),
    ("(b) casual greeting",
     "Всім привіт!",
     False),
    ("(b) casual emoji",
     "Дякую 👍",
     False),
    ("(c) unrelated VPN question",
     "як налаштувати VPN через Cisco ASA?",
     False),
    # Hallucination test: drone assembly ≠ frozen drone — must NOT answer with thermal bag tip
    ("(c) drone assembly — no matching case",
     "є інструкції, як збирати літачок?",
     False),
]


if __name__ == "__main__":
    print(f"Bot: {BASE}")
    print(f"Group: {GROUP_ID}")
    print()

    print("=== SCRAG contents ===")
    for q in ["stabx", "GPS дрон", "дрон замерз", "VPN Cisco"]:
        cases = retrieve(q)
        print(f"  {q!r}: {len(cases)} case(s)")
        for c in cases:
            d = c.get("distance", 9)
            doc = (c.get("document") or "")[:70]
            print(f"    dist={d:.3f}  {doc!r}")
    print()

    print("=== RAG scenario tests (label, expected has_case_link) ===")
    results = []
    for label, q, want_link in SCENARIOS:
        print(f"\n{label}")
        print(f"  Q: {q[:80]!r}")
        try:
            res = answer(q)
            resp = res.get("response", "")
            has_link = res.get("has_case_link", False)
            is_admin = res.get("is_admin_tag", False)
            scrag = res.get("scrag_hits", 0)
            print(f"  scrag={scrag}  has_link={has_link}  is_admin={is_admin}")
            print(f"  Response: {resp[:120]!r}")
            ok = has_link == want_link
            print(f"  {'✓ PASS' if ok else '✗ FAIL (expected has_link=' + str(want_link) + ')'}")
        except Exception as e:
            ok = False
            print(f"  ✗ ERROR: {e}")
        results.append((label, ok))

    print("\n=== Summary ===")
    all_ok = True
    for label, ok in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {label}")
        if not ok:
            all_ok = False
    sys.exit(0 if all_ok else 1)
