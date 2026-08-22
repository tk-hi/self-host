"""Generate the synthetic Meridian Search corpus + ENTITY_MANIFEST.json.

Entities (names, HKIDs, phones, emails, amounts) are created in Python and
injected verbatim into documents; the local vLLM only writes flavor prose
around them. This makes the manifest complete by construction — the ground
truth for every downstream leak audit.

Run on the instance:  /workspace/venvs/pipeline/bin/python gen_corpus.py
"""

import json
import os
import random
import re
import urllib.request
from pathlib import Path

random.seed(20260823)

OUT = Path("/workspace/corpus")
VLLM = "http://127.0.0.1:8000/v1/chat/completions"
VLLM_KEY = os.environ.get("VLLM_API_KEY", "")
MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3.8-27b-uncensored")

EN_FIRST = ["Aidan", "Beatrice", "Calvin", "Dorothy", "Elliot", "Fiona",
            "Gareth", "Hazel", "Ivan", "Jocelyn", "Kelvin", "Lorraine",
            "Marcus", "Natalie", "Oscar", "Priscilla", "Quentin", "Rosalind",
            "Stanley", "Tiffany", "Ulysses", "Vivian", "Wesley", "Xenia",
            "Yannick", "Zoe", "Bernard", "Cassandra", "Desmond", "Estella",
            "Frederick", "Gwendolyn", "Horace", "Isadora", "Jerome", "Katrina",
            "Leopold", "Miranda", "Nathaniel", "Ophelia"]
EN_LAST = ["Chan", "Wong", "Cheung", "Lau", "Ng", "Leung", "Ho", "Yip",
           "Tsang", "Fung", "Kwok", "Lam", "Siu", "Tam", "Yuen", "Mak",
           "Chow", "Poon", "Szeto", "Auyeung"]
ZH_NAMES = ["陳嘉偉", "黃詩琪", "張俊傑", "劉曉彤", "吳家豪", "梁美儀", "何志明",
            "葉婉婷", "曾德華", "馮嘉欣", "郭永康", "林淑芬", "蕭建邦", "譚穎欣",
            "阮世昌", "麥麗珊", "周文傑", "潘曉琳", "司徒駿", "歐陽雪"]
ROLES = ["Head of Compliance", "Senior Relationship Manager", "VP Technology",
         "Finance Director", "Operations Manager", "Chief Risk Officer",
         "Data Engineering Lead", "Treasury Analyst", "Legal Counsel",
         "HR Business Partner", "Internal Audit Manager", "Product Director"]
CLIENT_NAMES = ["Atlas Global Bank", "Harbourline Securities", "Pinnacle Re",
                "Kowloon Digital Holdings", "Victoria Peak Capital",
                "Silvermount Insurance", "Causeway Fintech", "Orient Meridian Trust",
                "Jade Basin Logistics", "Talbot & Wing LLP",
                "Redhill Commodities", "Stanley Bay Asset Management"]


def hkid():
    letter = random.choice("ABCDEFGHJKMNPRSTVWXYZ")
    digits = [random.randint(0, 9) for _ in range(6)]
    vals = [ord(letter) - 55] + digits
    weights = [8, 7, 6, 5, 4, 3, 2]
    s = 9 * 36 + sum(v * w for v, w in zip(vals, weights))
    check = (11 - s % 11) % 11
    return f"{letter}{''.join(map(str, digits))}({'A' if check == 10 else check})"


def hk_phone():
    return f"+852 {random.choice([5, 6, 9])}{random.randint(100, 999)} {random.randint(1000, 9999)}"


def llm(prompt, max_tokens=500):
    req = urllib.request.Request(
        VLLM,
        json.dumps({"model": MODEL, "max_tokens": max_tokens,
                    "temperature": 0.9, "top_p": 0.95,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "user", "content": prompt}]}).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {VLLM_KEY}"})
    out = json.load(urllib.request.urlopen(req, timeout=300))
    return out["choices"][0]["message"]["content"].strip()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "docs").mkdir(exist_ok=True)
    manifest = {"persons": [], "person_names_zh": [], "orgs": [], "hkids": [],
                "phones": [], "emails": [], "amounts": []}

    # --- candidates ---
    candidates = []
    pairs = random.sample([(f, l) for f in EN_FIRST for l in EN_LAST], 40)
    for i, (first, last) in enumerate(pairs):
        c = {
            "id": f"CAND-{i+1:03d}",
            "name": f"{first} {last}",
            "zh_name": ZH_NAMES[i % len(ZH_NAMES)] if i % 2 == 0 else None,
            "hkid": hkid(),
            "phone": hk_phone(),
            "email": f"{first.lower()}.{last.lower()}{random.randint(1,99)}@gmail.com",
            "role": random.choice(ROLES),
            "salary_history": [random.randrange(55, 185) * 10000
                               for _ in range(random.randint(2, 4))],
        }
        candidates.append(c)
        manifest["persons"].append(c["name"])
        if c["zh_name"]:
            manifest["person_names_zh"].append(c["zh_name"])
        manifest["hkids"].append(c["hkid"])
        manifest["phones"].append(c["phone"])
        manifest["emails"].append(c["email"])
        manifest["amounts"] += [f"HK${s:,}" for s in c["salary_history"]]

    # --- clients ---
    clients = []
    for i, cname in enumerate(CLIENT_NAMES):
        first, last = random.choice(EN_FIRST), random.choice(EN_LAST)
        cl = {
            "id": f"CLI-{i+1:02d}", "name": cname,
            "contact": f"{first} {last}",
            "contact_email": f"{first.lower()}.{last.lower()}@{re.sub('[^a-z]', '', cname.lower())[:12]}.com.hk",
            "contact_phone": hk_phone(),
            "fee_pct": random.randint(18, 25),
            "rebate": random.choice(["50% rebate if candidate leaves within 3 months",
                                     "sliding 60/30/10 rebate over 90 days",
                                     "full refund within 8 weeks, then 25%",
                                     "no rebate; replacement guarantee only"]),
            "anchor": cname == "Atlas Global Bank",
        }
        clients.append(cl)
        manifest["orgs"].append(cl["name"])
        manifest["persons"].append(cl["contact"])
        manifest["emails"].append(cl["contact_email"])
        manifest["phones"].append(cl["contact_phone"])

    # --- placements ---
    placements = []
    for i in range(60):
        cand = random.choice(candidates)
        cl = clients[0] if i < 10 else random.choice(clients)  # anchor gets volume
        salary = random.randrange(60, 190) * 10000
        fee = int(salary * 12 * cl["fee_pct"] / 100)
        placements.append({
            "id": f"PLC-{i+1:03d}", "candidate": cand, "client": cl,
            "role": random.choice(ROLES), "monthly_salary": salary, "fee": fee,
            "date": f"2026-{random.randint(1,7):02d}-{random.randint(1,28):02d}",
        })
        manifest["amounts"] += [f"HK${salary:,}", f"HK${fee:,}"]

    # --- documents (LLM prose + code-injected entities) ---
    def write_doc(doc_id, title, body):
        (OUT / "docs" / f"{doc_id}.txt").write_text(f"{title}\n\n{body}")

    print("generating candidate profiles...")
    for c in candidates:
        prose = llm(f"Write 2 short paragraphs of a recruiter's internal notes about "
                    f"a Hong Kong-based {c['role']} candidate: career trajectory and "
                    f"placement considerations. Do NOT invent any names, numbers, "
                    f"companies, or contact details — write generically.", 300)
        body = (f"Candidate profile {c['id']}\nName: {c['name']}"
                + (f" ({c['zh_name']})" if c["zh_name"] else "") + "\n"
                f"HKID: {c['hkid']}\nPhone: {c['phone']}\nEmail: {c['email']}\n"
                f"Current target role: {c['role']}\n"
                f"Salary history (monthly): "
                + ", ".join(f"HK${s:,}" for s in c["salary_history"]) + "\n\n" + prose)
        write_doc(c["id"], f"Candidate profile — {c['name']}", body)

    print("generating client contracts...")
    for cl in clients:
        prose = llm("Write 2 paragraphs of generic terms-of-business boilerplate for "
                    "a recruitment agency contract (confidentiality, non-solicit). "
                    "No names or numbers.", 300)
        body = (f"Terms of business {cl['id']}\nClient: {cl['name']}\n"
                f"Primary contact: {cl['contact']} <{cl['contact_email']}> "
                f"{cl['contact_phone']}\n"
                f"Placement fee: {cl['fee_pct']}% of first-year base salary\n"
                f"Rebate terms: {cl['rebate']}\n"
                + ("Status: MANAGED ACCOUNT (anchor client)\n" if cl["anchor"] else "")
                + "\n" + prose)
        write_doc(cl["id"], f"Terms of business — {cl['name']}", body)

    print("generating placement records...")
    for p in placements:
        body = (f"Placement record {p['id']}\n"
                f"Candidate: {p['candidate']['name']} ({p['candidate']['id']})\n"
                f"Client: {p['client']['name']} ({p['client']['id']})\n"
                f"Role: {p['role']}\nStart date: {p['date']}\n"
                f"Monthly salary: HK${p['monthly_salary']:,}\n"
                f"Fee invoiced ({p['client']['fee_pct']}% of annual): HK${p['fee']:,}\n"
                f"Rebate terms applied: {p['client']['rebate']}\n")
        write_doc(p["id"], f"Placement {p['id']}", body)

    print("generating misc docs...")
    misc_topics = [
        ("meeting notes: quarterly review of the Atlas Global Bank managed account, "
         "fee performance and renewal risks", clients[0]),
        ("internal email thread about raising fees with underperforming clients", None),
        ("meeting notes on rebate policy exposure across banking clients", None),
        ("draft memo on 2026 salary benchmarks for compliance roles in HK", None),
    ] * 5
    for i, (topic, cl) in enumerate(misc_topics[:20]):
        prose = llm(f"Write ~200 words of {topic}. Do NOT invent names, companies or "
                    f"figures — refer to parties generically.", 400)
        inject = ""
        if cl:
            inject = (f"\nAccount: {cl['name']} — contact {cl['contact']} "
                      f"<{cl['contact_email']}>. Standing fee {cl['fee_pct']}%, "
                      f"{cl['rebate']}.\n")
        write_doc(f"MISC-{i+1:02d}", f"Internal document MISC-{i+1:02d}",
                  inject + "\n" + prose)

    # --- manifest ---
    for k in manifest:
        manifest[k] = sorted(set(manifest[k]))
    total = sum(len(v) for v in manifest.values())
    manifest["_total_entities"] = total
    (OUT / "ENTITY_MANIFEST.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    json.dump({"candidates": [c["id"] for c in candidates],
               "clients": [c["id"] for c in clients],
               "placements": [p["id"] for p in placements]},
              open(OUT / "corpus_index.json", "w"), indent=1)
    print(f"DONE: {len(list((OUT/'docs').glob('*.txt')))} docs, {total} manifest entities")


if __name__ == "__main__":
    main()
