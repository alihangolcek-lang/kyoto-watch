#!/usr/bin/env python3
"""Kyoto Apartment oda takibi - GitHub Actions surumu.

Sayfayi ceker, onceki state.json ile karsilastirir, yeni/degismis odalari
bulur ve oncelik kademesine gore mail atar. State repoya geri commit edilir.

Oncelik kademeleri (STUDIO > PAYLASIMLI):
  1  musait + STUDIO      -> kirmizi, "hemen rezervasyon yap"
  2  musait + PAYLASIMLI  -> turuncu, ikinci tercih
  3/4  yakinda musait     -> gri, bilgi

Gerekli ortam degiskenleri (GitHub Secrets):
  MAIL_USERNAME  gonderen Gmail adresi
  MAIL_PASSWORD  Gmail UYGULAMA SIFRESI (normal sifre degil)
  MAIL_TO        alici adres
"""
import datetime
import json
import os
import re
import smtplib
import sys
import urllib.request
from email.message import EmailMessage

URL = "https://www.kyoto-apartment.com/en/search"
STATE = "state.json"
HEALTH = "health.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td.*?>(.*?)</td>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
TYPE_ICONS = (("icon-oneroom", "STUDIO"), ("icon-sharehouse", "PAYLASIMLI"))

FAIL_ESCALATE = 3
PARSE_ESCALATE = 2
ALERT_EVERY = 6

_PROP_CACHE = {}


def clean(h):
    t = TAG_RE.sub(" ", h)
    t = t.replace("&yen;", "¬•").replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", t).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s" % r.status)
        return r.read().decode("utf-8", "replace")


def reservation_link(prop_url, room_no):
    """Ilan sayfasindaki 'Reservation' butonunun hedefi.

    /en/inquiry?linkPrevcID=<id> formu apartman adi + oda no dolu acilir.
    """
    try:
        if prop_url not in _PROP_CACHE:
            _PROP_CACHE[prop_url] = fetch(prop_url)
        html = _PROP_CACHE[prop_url]
    except Exception:
        return prop_url
    for row in ROW_RE.findall(html):
        if "linkPrevcID" not in row:
            continue
        cells = [clean(c) for c in CELL_RE.findall(row)]
        if cells and cells[0].split()[0] == room_no:
            m = re.search(r"linkPrevcID=(\d+)", row)
            if m:
                return ("https://www.kyoto-apartment.com/en/inquiry"
                        "?linkPrevcID=" + m.group(1))
    return prop_url


def parse(html):
    rooms = {}
    for tid, bucket in (("myTable", "AVAILABLE"), ("myTable2", "AVAILABLE SOON")):
        m = re.search(r'id="%s".*?<tbody>(.*?)</tbody>' % tid, html, re.S)
        if not m:
            return None
        for row in ROW_RE.findall(m.group(1)):
            raw = CELL_RE.findall(row)
            cells = [clean(c) for c in raw]
            if len(cells) < 5 or not cells[0]:
                continue
            rtype = "?"
            for cls, lab in TYPE_ICONS:
                if cls in raw[1]:
                    rtype = lab
                    break
            link = re.search(r'href="([^"]+)"', row)
            rooms[bucket + " | " + cells[0]] = {
                "bucket": bucket, "room": cells[0], "rtype": rtype,
                "status": cells[2], "date": cells[3], "rent": cells[4],
                "link": link.group(1) if link else URL,
            }
    return rooms


def tier_of(bucket, rtype):
    if bucket == "AVAILABLE":
        return 1 if rtype == "STUDIO" else 2
    return 3 if rtype == "STUDIO" else 4


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def should_alert(streak, threshold):
    if streak < threshold:
        return False
    return streak == threshold or (streak - threshold) % ALERT_EVERY == 0


def health(ok, err=""):
    h = {"fail_streak": 0, "last_error": "", "last_ok": "", "last_check": ""}
    h.update(load(HEALTH, {}))
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")
    h["last_check"] = now
    if ok:
        h.update({"fail_streak": 0, "last_ok": now, "last_error": ""})
    else:
        h["fail_streak"] = int(h.get("fail_streak") or 0) + 1
        h["last_error"] = err
    save(HEALTH, h)
    return h


# ----------------------------------------------------------------- mail

BTN = ('<table cellpadding="0" cellspacing="0" border="0" style="margin:14px 0;">'
       '<tr><td align="center" bgcolor="{bg}" style="border-radius:6px;">'
       '<a href="{url}" style="display:inline-block;padding:{pad};'
       'font-family:Arial,Helvetica,sans-serif;font-size:{fs};font-weight:bold;'
       'color:#ffffff;text-decoration:none;border-radius:6px;">{txt}</a>'
       '</td></tr></table>')

NOTE = ('<div style="font-size:12px;color:#888;margin-bottom:9px;">'
        'Form apartman adƒ± ve oda numarasƒ± dolu a√ßƒ±lƒ±r. Kalan alanlar i√ßin yer imi '
        '√ßubuƒüundaki &quot;Kyoto formu doldur&quot; bookmarklet\'ine tƒ±kla. '
        'Sonra g√∂zden ge√ßirip Submit\'e bas.</div>')

STYLE = {
    1: ("#d32f2f", "15px 34px", "17px", "‚ö° HEMEN REZERVE ET"),
    2: ("#ef6c00", "13px 28px", "16px", "Rezerve et"),
    3: ("#5f6a7d", "11px 22px", "14px", "Rezervasyon formu"),
    4: ("#5f6a7d", "11px 22px", "14px", "Rezervasyon formu"),
}
TIER_NAME = {1: "Studio ¬∑ ≈üu an m√ºsait", 2: "Payla≈üƒ±mlƒ± ¬∑ ≈üu an m√ºsait",
             3: "Studio ¬∑ yakƒ±nda", 4: "Payla≈üƒ±mlƒ± ¬∑ yakƒ±nda"}


def card(r, tier):
    bg, pad, fs, txt = STYLE[tier]
    tip = "Studio Apartment" if r["rtype"] == "STUDIO" else "Payla≈üƒ±mlƒ± (Shared house)"
    return (
        '<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px 19px;'
        'margin-bottom:16px;">'
        '<div style="font-size:%s;font-weight:bold;margin-bottom:9px;">%s</div>'
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="font-size:14px;color:#444;">'
        '<tr><td style="padding:2px 12px 2px 0;color:#777;">Tip</td><td>%s</td></tr>'
        '<tr><td style="padding:2px 12px 2px 0;color:#777;">Kira</td>'
        '<td><strong>%s / ay</strong></td></tr>'
        '<tr><td style="padding:2px 12px 2px 0;color:#777;">Durum</td><td>%s</td></tr>'
        '<tr><td style="padding:2px 12px 2px 0;color:#777;">M√ºsait tarih</td>'
        '<td>%s</td></tr></table>%s%s'
        '<div style="font-size:13px;"><a href="%s" style="color:#1565c0;">'
        'Odanƒ±n ilan sayfasƒ±</a></div></div>'
        % ("19px" if tier == 1 else "17px", r["room"], tip, r["rent"],
           r["status"], r["date"] if r["date"].strip("- ") else "Hemen",
           BTN.format(bg=bg, url=r["reserve"], pad=pad, fs=fs,
                      txt="%s ‚Äî %s" % (txt, r["room"])),
           NOTE, r["link"]))


def build_mail(groups, top):
    heads = {
        1: ("üî•üè† STUDIO M√úSAƒ∞T ‚Äî HEMEN REZERVASYON YAP! (%d adet)",
            "üî• Bƒ∞Rƒ∞NCƒ∞ TERCƒ∞Hƒ∞N √áIKTI ‚Äî M√úSAƒ∞T STUDIO APARTMENT. Hemen rezervasyon yap:",
            "#c62828"),
        2: ("üö® M√ºsait oda var ‚Äî payla≈üƒ±mlƒ± (2. tercih) (%d adet)",
            "≈ûu an kiralanabilir oda var. Payla≈üƒ±mlƒ±, yani ikinci tercihin ‚Äî "
            "studio beklemek istersen acele etmene gerek yok, ama oda hƒ±zlƒ± gidebilir:",
            "#e65100"),
        3: ("üîî Kyoto Apartment: yakƒ±nda m√ºsait oda deƒüi≈üikliƒüi (%d adet)",
            "Bilgi ama√ßlƒ± ‚Äî ileri tarihli listede deƒüi≈üiklik var (acil deƒüil):",
            "#455a64"),
    }
    key = top if top in heads else 3
    subj_t, intro, col = heads[key]
    subject = subj_t % len(groups[top])

    html = ['<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;'
            'color:#222;">',
            '<p style="font-size:16px;font-weight:bold;color:%s;margin:0 0 18px;">%s</p>'
            % (col, intro)]
    text = [intro, ""]
    for tier in (1, 2, 3, 4):
        rooms = groups.get(tier)
        if not rooms:
            continue
        if tier != top:
            html.append('<h3 style="font-size:14px;color:#555;margin:26px 0 10px;'
                        'padding-top:14px;border-top:1px solid #eee;">%s</h3>'
                        % TIER_NAME[tier])
            text.append("--- %s ---" % TIER_NAME[tier])
        for r in rooms:
            html.append(card(r, tier))
            text.append("%s | %s | %s | %s\n  REZERVE: %s\n  Ilan: %s"
                        % (r["room"], r["rtype"], r["rent"], r["status"],
                           r["reserve"], r["link"]))
        text.append("")
    html.append('<p style="font-size:13px;color:#777;border-top:1px solid #eee;'
                'padding-top:12px;">Takip 10 dakikada bir GitHub Actions √ºzerinde '
                '√ßalƒ±≈üƒ±yor.<br><a href="%s" style="color:#1565c0;">Arama sayfasƒ±</a>'
                '</p></div>' % URL)
    text.append("Arama sayfasi: " + URL)
    return subject, "\n".join(text), "".join(html)


def send(subject, text, html):
    user, pw, to = (os.environ.get("MAIL_USERNAME"),
                    os.environ.get("MAIL_PASSWORD"),
                    os.environ.get("MAIL_TO"))
    if not (user and pw and to):
        print("HATA: MAIL_USERNAME / MAIL_PASSWORD / MAIL_TO secret'lari eksik",
              file=sys.stderr)
        return False
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, user, to
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(m)
    print("mail gonderildi -> %s | %s" % (to, subject))
    return True


# ----------------------------------------------------------------- main

def main():
    try:
        html = fetch(URL)
    except Exception as e:
        h = health(False, "fetch failed: %s" % e)
        print("fetch failed: %s (ust uste %d)" % (e, h["fail_streak"]),
              file=sys.stderr)
        if should_alert(h["fail_streak"], FAIL_ESCALATE):
            send("‚ö†Ô∏è Kyoto Apartment takibi √áALI≈ûMIYOR ‚Äî oda bildirimleri durdu",
                 "Oda takibi calismiyor. Yeni oda cikSA BILE mail GELMEYECEK.\n\n"
                 "Sebep: %s\nUst uste basarisiz tur: %d\nSon basarili kontrol: %s\n\n"
                 "Siteye erisim engellenmis veya site kapali olabilir.\n"
                 "Bu arada odalari elle kontrol et: %s"
                 % (h["last_error"], h["fail_streak"], h["last_ok"] or "hic", URL),
                 "")
        return 1

    current = parse(html)
    if current is None:
        h = health(False, "parse failed: room tables not found")
        print("parse failed (ust uste %d)" % h["fail_streak"], file=sys.stderr)
        if should_alert(h["fail_streak"], PARSE_ESCALATE):
            send("‚ö†Ô∏è Kyoto Apartment takibi BOZULDU ‚Äî sayfa yapƒ±sƒ± deƒüi≈üti",
                 "Sayfadaki oda tablolari bulunamadi, site yapisi degismis.\n"
                 "watch.py guncellenmeli.\n\nSon basarili kontrol: %s\n"
                 "Elle kontrol: %s" % (h["last_ok"] or "hic", URL), "")
        return 1

    health(True)
    first = not os.path.exists(STATE)
    prev = load(STATE, {})
    if not prev:
        first = True

    new = [k for k in current if k not in prev]
    chg = [k for k in current if k in prev
           and (current[k]["status"] != prev[k].get("status")
                or current[k]["rtype"] != prev[k].get("rtype",
                                                      current[k]["rtype"]))]
    save(STATE, current)

    if first:
        print("baseline kaydedildi: %d oda" % len(current))
        return 0

    changed = new + chg
    if not changed:
        print("degisiklik yok (%d oda izleniyor)" % len(current))
        return 0

    groups = {}
    for k in sorted(changed, key=lambda x: tier_of(current[x]["bucket"],
                                                   current[x]["rtype"])):
        r = dict(current[k])
        r["reserve"] = reservation_link(r["link"], r["room"].split("/")[-1].strip())
        groups.setdefault(tier_of(r["bucket"], r["rtype"]), []).append(r)

    top = min(groups)
    subject, text, htmlbody = build_mail(groups, top)
    print("kademe %d, %d oda -> %s" % (top, len(changed), subject))
    send(subject, text, htmlbody)
    return 0


if __name__ == "__main__":
    sys.exit(main())
