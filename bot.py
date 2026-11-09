import asyncio
import re
import aiohttp
import random
import string
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import os
import time

# ================= CONFIG =================
BOT_TOKEN = "8939694046:AAGm9QzRRxji-Pvj-LsvxvnjEZv7_9cOG5s" # Replace with your token
WATERMARK = "@ZYELEO"
# ==========================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- STATES ---
class CheckerStates(StatesGroup):
    waiting_for_min_price = State()
    waiting_for_max_price = State()
    waiting_for_sites = State()

class ScraperStates(StatesGroup):
    waiting_for_target_amount = State()
    waiting_for_scrape_sites = State()

class MakerStates(StatesGroup):
    waiting_for_maker_amount = State()
    waiting_for_maker_count = State()
    waiting_for_maker_sites = State()

# --- HEADERS TO BYPASS CLOUDFLARE ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# --- MAIN CHECKER FUNCTION ---
async def check_shopify_site(session, raw_url, min_price, max_price):
    domain = raw_url.strip().lower()
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    
    result = {
        "domain": domain,
        "status": "Unknown",
        "graphql": "Unknown",
        "captcha": "Unknown",
        "price_found": "N/A",
        "in_range": False,
        "raw_price": 0.0
    }

    try:
        async with session.get(f"https://{domain}", headers=HEADERS, timeout=15, ssl=False) as response:
            if response.status != 200:
                result["status"] = f"Dead ({response.status})"
                return result
                
            html = await response.text()

            is_shopify = any(sig in html.lower() for sig in ["cdn.shopify.com", "shopify.theme", "shopify-payment-button", "myshopify.com"])
            if not is_shopify:
                result["status"] = "Dead (Not Shopify)"
                return result

            result["status"] = "Live"

            captcha_patterns = ["hcaptcha", "recaptcha", "captcha-container", "cf-turnstile", "g-recaptcha"]
            result["captcha"] = "Captcha" if any(p in html.lower() for p in captcha_patterns) else "Uncaptcha"

            gql_url = f"https://{domain}/api/2023-10/graphql.json"
            gql_query = """
            {
              products(first: 1) {
                edges {
                  node {
                    title
                    priceRangeV2 {
                      minVariantPrice {
                        amount
                        currencyCode
                      }
                    }
                  }
                }
              }
            }
            """
            async with session.post(gql_url, json={"query": gql_query}, headers={"Content-Type": "application/json"}, ssl=False, timeout=10) as gql_resp:
                if gql_resp.status == 200:
                    gql_data = await gql_resp.json()
                    try:
                        amount_str = gql_data['data']['products']['edges'][0]['node']['priceRangeV2']['minVariantPrice']['amount']
                        currency = gql_data['data']['products']['edges'][0]['node']['priceRangeV2']['minVariantPrice']['currencyCode']
                        
                        price_val = float(amount_str)
                        result["raw_price"] = price_val
                        result["price_found"] = f"{price_val} {currency}"
                        result["graphql"] = "Valid GraphQL"
                        
                        if min_price <= price_val <= max_price:
                            result["in_range"] = True
                    except (KeyError, IndexError, TypeError):
                        result["graphql"] = "No Products Found"
                else:
                    result["graphql"] = "GraphQL Blocked"

        async with session.get(f"https://{domain}/cart/1:1", headers=HEADERS, timeout=10, ssl=False, allow_redirects=True) as checkout_resp:
            if "/checkout" in str(checkout_resp.url) or checkout_resp.status == 200:
                result["status"] = "Live + Checkout OK"
            else:
                result["status"] = "Live (Checkout Issue)"

    except asyncio.TimeoutError:
        result["status"] = "Dead (Timeout)"
    except Exception as e:
        result["status"] = "Dead (Error)"

    return result

# --- SCRAPER CORE: find sites with EXACT price amount ---
async def scrape_site_by_amount(session, raw_url, target_amount, tolerance=0.01):
    domain = raw_url.strip().lower()
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    out = {
        "domain": domain,
        "status": "Unknown",
        "matched": False,
        "price": 0.0,
        "currency": "?",
        "caption": "",
        "captcha": "Unknown",
        "graphql": "Unknown",
    }

    try:
        async with session.get(f"https://{domain}", headers=HEADERS, timeout=15, ssl=False) as resp:
            if resp.status != 200:
                out["status"] = f"Dead ({resp.status})"
                return out
            html = await resp.text()

        is_shopify = any(sig in html.lower() for sig in ["cdn.shopify.com", "shopify.theme", "shopify-payment-button", "myshopify.com"])
        if not is_shopify:
            out["status"] = "Dead (Not Shopify)"
            return out

        out["status"] = "Live"
        captcha_patterns = ["hcaptcha", "recaptcha", "captcha-container", "cf-turnstile", "g-recaptcha"]
        out["captcha"] = "Captcha" if any(p in html.lower() for p in captcha_patterns) else "Uncaptcha"

        gql_url = f"https://{domain}/api/2023-10/graphql.json"
        gql_query = """
        {
          products(first: 5) {
            edges {
              node {
                title
                priceRangeV2 {
                  minVariantPrice { amount currencyCode }
                }
              }
            }
          }
        }
        """
        async with session.post(gql_url, json={"query": gql_query}, headers={"Content-Type": "application/json"}, ssl=False, timeout=10) as gql_resp:
            if gql_resp.status != 200:
                out["graphql"] = "GraphQL Blocked"
                return out
            gql_data = await gql_resp.json()

        try:
            edges = gql_data['data']['products']['edges']
            if not edges:
                out["graphql"] = "No Products Found"
                return out

            # Find closest matching product to target amount
            best = None
            for edge in edges:
                node = edge["node"]
                try:
                    amt = float(node["priceRangeV2"]["minVariantPrice"]["amount"])
                    cur = node["priceRangeV2"]["minVariantPrice"]["currencyCode"]
                    title = node.get("title", "Product")
                    if abs(amt - target_amount) <= tolerance:
                        best = {"amount": amt, "currency": cur, "title": title}
                        break
                    if best is None or abs(amt - target_amount) < abs(best["amount"] - target_amount):
                        best = {"amount": amt, "currency": cur, "title": title}
                except (KeyError, TypeError, ValueError):
                    continue

            if best is None:
                out["graphql"] = "No Parseable Prices"
                return out

            out["price"] = best["amount"]
            out["currency"] = best["currency"]
            out["caption"] = best["title"]
            out["graphql"] = "Valid GraphQL"
            out["matched"] = abs(best["amount"] - target_amount) <= tolerance

        except (KeyError, IndexError, TypeError):
            out["graphql"] = "Invalid Response"

    except asyncio.TimeoutError:
        out["status"] = "Dead (Timeout)"
    except Exception:
        out["status"] = "Dead (Error)"

    return out


# --- BOT HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Range Checker", callback_data="start_check")],
        [InlineKeyboardButton(text="🔍 Scrape by Amount", callback_data="scrape_by_amount")],
        [InlineKeyboardButton(text="🛠 Custom Amount Maker", callback_data="custom_maker")],
        [InlineKeyboardButton(text="📊 Bot Info", callback_data="bot_info")]
    ])
    await message.answer(
        f"Welcome to **Shopify Price Checker**!\nPowered by {WATERMARK}\n\n"
        "Choose a mode:\n"
        "• 🚀 **Range Checker** – filter sites by min/max price\n"
        "• 🔍 **Scrape by Amount** – find sites with an exact price\n"
        "• 🛠 **Custom Amount Maker** – auto-build a `.txt` of sites at your chosen amount",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "bot_info")
async def bot_info(callback: CallbackQuery):
    await callback.message.answer(
        f"**Shopify Toolkit**\n\n"
        "• Range Checker – live/dead + GraphQL + captcha + price range\n"
        "• Scraper – exact-price detection\n"
        "• Custom Maker – auto-generates a ready-to-use site list at your target amount\n\n"
        f"👑 Owner: {WATERMARK}",
        parse_mode="Markdown"
    )
    await callback.answer()

# ---------- RANGE CHECKER (original) ----------

@dp.callback_query(F.data == "start_check")
async def ask_min_price(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 **Step 1: Enter Minimum Price**\n(Example: 10)", parse_mode="Markdown")
    await state.set_state(CheckerStates.waiting_for_min_price)
    await callback.answer()

@dp.message(CheckerStates.waiting_for_min_price)
async def ask_max_price(message: Message, state: FSMContext):
    if not message.text.replace('.', '', 1).isdigit():
        await message.answer("❌ Please enter a valid number (e.g., 10 or 10.50).")
        return
    await state.update_data(min_price=float(message.text))
    await message.answer("💰 **Step 2: Enter Maximum Price**\n(Example: 100)", parse_mode="Markdown")
    await state.set_state(CheckerStates.waiting_for_max_price)

@dp.message(CheckerStates.waiting_for_max_price)
async def ask_sites(message: Message, state: FSMContext):
    if not message.text.replace('.', '', 1).isdigit():
        await message.answer("❌ Please enter a valid number.")
        return
    max_p = float(message.text)
    data = await state.get_data()
    min_p = data.get("min_price")
    if max_p < min_p:
        await message.answer("❌ Max price cannot be lower than Min price. Try again.")
        return
    await state.update_data(max_price=max_p)
    await message.answer(
        "📝 **Step 3: Send your sites**\n\nUpload a `.txt` file OR paste links below (one per line).\n"
        "**Accepts all formats:** `site.com`, `https://site.com`, `www.site.com`",
        parse_mode="Markdown"
    )
    await state.set_state(CheckerStates.waiting_for_sites)

@dp.message(CheckerStates.waiting_for_sites)
async def process_sites(message: Message, state: FSMContext):
    data = await state.get_data()
    min_p = data.get("min_price", 0.0)
    max_p = data.get("max_price", 999999.0)

    sites = []
    if message.document:
        if not message.document.file_name.endswith('.txt'):
            await message.answer("❌ Please upload a `.txt` file.")
            return
        file_path = f"temp_{message.document.file_id}.txt"
        await bot.download(message.document, destination=file_path)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            sites = [line.strip() for line in f if line.strip()]
        os.remove(file_path)
    else:
        sites = [line.strip() for line in message.text.split('\n') if line.strip()]

    if not sites:
        await message.answer("❌ No valid sites found. Send again.")
        return

    msg = await message.answer(f"⏳ **Checking {len(sites)} sites...**\nRange: {min_p} to {max_p}\nPlease wait...", parse_mode="Markdown")

    results, live_count, dead_count, upprice_count, target_count = [], 0, 0, 0, 0

    timestamp = int(time.time())
    upprice_file = "uppricelive.txt"
    target_file = "clean_target_live.txt"
    dead_file = f"dead_{timestamp}.txt"

    for f in [upprice_file, target_file, dead_file]:
        if os.path.exists(f):
            os.remove(f)

    with open(upprice_file, "w", encoding="utf-8") as f:
        f.write(f"--- LIVE SITES OUT OF RANGE (Upprice) | {WATERMARK} ---\n\n")
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(f"--- TARGET LIVE SITES ({min_p} - {max_p}) | {WATERMARK} ---\n\n")
    with open(dead_file, "w", encoding="utf-8") as f:
        f.write(f"--- DEAD SITES | {WATERMARK} ---\n\n")

    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_shopify_site(session, site, min_p, max_p) for site in sites]
        completed = await asyncio.gather(*tasks)

        for res in completed:
            cap_str = "Captcha" if res['captcha'] == 'Captcha' else "Uncaptcha"
            line = f"[{res['status']}] | Price: {res['price_found']} | {cap_str} | {res['domain']}"

            if "Dead" in res['status']:
                dead_count += 1
                with open(dead_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            elif res['in_range']:
                target_count += 1
                with open(target_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                results.append(f"✅ `{res['domain']}`\n┣ {res['graphql']} | {cap_str}\n┗ Price: {res['price_found']}")
            else:
                upprice_count += 1
                with open(upprice_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

    summary = (
        f"📊 **Checking Complete!**\n👑 By: {WATERMARK}\n\n"
        f"🟢 Total Live: {target_count + upprice_count}\n"
        f"🎯 In Range ({min_p} - {max_p}): {target_count}\n"
        f"📈 Out of Range (Upprice): {upprice_count}\n"
        f"🔴 Dead/Invalid: {dead_count}\n\n"
    )
    summary += "**Preview (Target Sites):**\n\n" + ("\n\n".join(results[:5]) if results else "❌ No sites found in this price range.")
    await msg.edit_text(summary, parse_mode="Markdown")

    await message.answer_document(FSInputFile(target_file), caption=f"🎯 **Target Live Sites** (Clean)\n👑 {WATERMARK}")
    await message.answer_document(FSInputFile(upprice_file), caption=f"📈 **Upprice Live Sites**\n👑 {WATERMARK}")
    await message.answer_document(FSInputFile(dead_file), caption=f"❌ **Dead Sites**\n👑 {WATERMARK}")
    await state.clear()

# ---------- SCRAPER BY AMOUNT ----------

@dp.callback_query(F.data == "scrape_by_amount")
async def scrape_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🔍 **Scrape by Amount**\n\n"
        "Send the **exact price** you want to find (e.g. `19.99`).\n"
        "The bot will scan each site and match products at that price.",
        parse_mode="Markdown"
    )
    await state.set_state(ScraperStates.waiting_for_target_amount)
    await callback.answer()

@dp.message(ScraperStates.waiting_for_target_amount)
async def scrape_amount_received(message: Message, state: FSMContext):
    txt = message.text.strip().replace(",", ".")
    try:
        amount = float(txt)
    except ValueError:
        await message.answer("❌ Invalid amount. Send a number like `19.99`.")
        return
    await state.update_data(target_amount=amount)
    await message.answer(
        f"🎯 Target: **{amount}**\n\n"
        "Now send your sites:\n"
        "• Upload a `.txt` file, OR\n"
        "• Paste links (one per line)",
        parse_mode="Markdown"
    )
    await state.set_state(ScraperStates.waiting_for_scrape_sites)

@dp.message(ScraperStates.waiting_for_scrape_sites)
async def scrape_process(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target_amount", 0.0)

    sites = []
    if message.document:
        if not message.document.file_name.endswith('.txt'):
            await message.answer("❌ Please upload a `.txt` file.")
            return
        fp = f"temp_scrape_{message.document.file_id}.txt"
        await bot.download(message.document, destination=fp)
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            sites = [l.strip() for l in f if l.strip()]
        os.remove(fp)
    else:
        sites = [l.strip() for l in message.text.split('\n') if l.strip()]

    if not sites:
        await message.answer("❌ No sites provided.")
        return

    msg = await message.answer(
        f"🔎 **Scraping {len(sites)} sites for amount {target}...**\nPlease wait...",
        parse_mode="Markdown"
    )

    matches, dead, live_other = [], 0, 0
    stamp = int(time.time())
    match_file = f"scraped_{target}_{stamp}.txt"

    with open(match_file, "w", encoding="utf-8") as f:
        f.write(f"--- SCRAPED MATCHES @ {target} | {WATERMARK} ---\n\n")

    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [scrape_site_by_amount(session, s, target) for s in sites]
        results = await asyncio.gather(*tasks)

        for r in results:
            if "Dead" in r["status"]:
                dead += 1
                continue
            live_other += 1
            if r["matched"]:
                line = (f"{r['domain']} | Price: {r['price']} {r['currency']} | "
                        f"{r['captcha']} | {r['graphql']} | {r['caption']}")
                with open(match_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                matches.append(f"🎯 `{r['domain']}`\n┣ Price: {r['price']} {r['currency']}\n┗ {r['caption']}")

    summary = (
        f"🔍 **Scrape Complete!**\n👑 {WATERMARK}\n\n"
        f"🎯 Target amount: **{target}**\n"
        f"✅ Matches: {len(matches)}\n"
        f"🟢 Live (no match): {live_other - len(matches)}\n"
        f"🔴 Dead: {dead}\n\n"
    )
    summary += ("**Matches Preview:**\n\n" + "\n\n".join(matches[:10])) if matches else "❌ No sites matched that price."
    await msg.edit_text(summary, parse_mode="Markdown")

    if matches:
        await message.answer_document(
            FSInputFile(match_file),
            caption=f"🎯 **Scraped Sites @ {target}**\n👑 {WATERMARK}"
        )
    await state.clear()

# ---------- CUSTOM AMOUNT MAKER ----------

@dp.callback_query(F.data == "custom_maker")
async def maker_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🛠 **Custom Amount Maker**\n\n"
        "I'll scan your sites and build a **clean `.txt` list** of only the sites that sell a product at your chosen amount.\n\n"
        "**Step 1:** Send the target amount (e.g. `29.99`)",
        parse_mode="Markdown"
    )
    await state.set_state(MakerStates.waiting_for_maker_amount)
    await callback.answer()

@dp.message(MakerStates.waiting_for_maker_amount)
async def maker_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Invalid amount. Send a number like `29.99`.")
        return
    await state.update_data(maker_amount=amount)
    await message.answer(
        "**Step 2:** How many matching sites do you want? (max 200)\n"
        "Example: `10`",
        parse_mode="Markdown"
    )
    await state.set_state(MakerStates.waiting_for_maker_count)

@dp.message(MakerStates.waiting_for_maker_count)
async def maker_count(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Send a whole number, e.g. `10`.")
        return
    count = int(message.text.strip())
    if count < 1 or count > 200:
        await message.answer("❌ Choose between 1 and 200.")
        return
    await state.update_data(maker_count=count)
    await message.answer(
        "**Step 3:** Send your source sites:\n"
        "• Upload a `.txt` file, OR\n"
        "• Paste links (one per line)",
        parse_mode="Markdown"
    )
    await state.set_state(MakerStates.waiting_for_maker_sites)

@dp.message(MakerStates.waiting_for_maker_sites)
async def maker_process(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("maker_amount", 0.0)
    want = data.get("maker_count", 1)

    sites = []
    if message.document:
        if not message.document.file_name.endswith('.txt'):
            await message.answer("❌ Please upload a `.txt` file.")
            return
        fp = f"temp_maker_{message.document.file_id}.txt"
        await bot.download(message.document, destination=fp)
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            sites = [l.strip() for l in f if l.strip()]
        os.remove(fp)
    else:
        sites = [l.strip() for l in message.text.split('\n') if l.strip()]

    if not sites:
        await message.answer("❌ No sites provided.")
        return

    msg = await message.answer(
        f"🛠 **Building list of {want} sites @ {amount}...**\nScanning {len(sites)} sites.\nPlease wait...",
        parse_mode="Markdown"
    )

    found = []
    scanned = 0
    dead = 0
    stamp = int(time.time())
    out_file = f"custom_amount_{amount}_{stamp}.txt"

    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # shuffle for variety
        random.shuffle(sites)

        # process in small batches to stop early once enough matches found
        batch_size = 20
        for i in range(0, len(sites), batch_size):
            if len(found) >= want:
                break
            batch = sites[i:i + batch_size]
            tasks = [scrape_site_by_amount(session, s, amount) for s in batch]
            results = await asyncio.gather(*tasks)
            for r in results:
                scanned += 1
                if "Dead" in r["status"]:
                    dead += 1
                    continue
                if r["matched"]:
                    found.append(r)
                    if len(found) >= want:
                        break

    # write file
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(f"--- CUSTOM AMOUNT MAKER | Target: {amount} | Want: {want} | {WATERMARK} ---\n")
        f.write(f"--- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n\n")
        if found:
            for r in found:
                f.write(
                    f"{r['domain']} | Price: {r['price']} {r['currency']} | "
                    f"{r['captcha']} | {r['graphql']} | {r['caption']}\n"
                )
        else:
            f.write("No matching sites found.\n")

    preview = "\n".join(
        f"✅ `{r['domain']}` — {r['price']} {r['currency']} ({r['caption'][:30]})"
        for r in found[:10]
    ) or "❌ None matched."

    summary = (
        f"🛠 **Custom Amount Maker — Done!**\n👑 {WATERMARK}\n\n"
        f"🎯 Target amount: **{amount}**\n"
        f"📦 Wanted: {want} | ✅ Found: **{len(found)}**\n"
        f"🔍 Sites scanned: {scanned} | 🔴 Dead: {dead}\n\n"
        f"**Preview:**\n{preview}"
    )
    await msg.edit_text(summary, parse_mode="Markdown")

    if found:
        await message.answer_document(
            FSInputFile(out_file),
            caption=f"🛠 **Custom Amount List @ {amount}**\n{len(found)} matching sites\n👑 {WATERMARK}"
        )
    else:
        await message.answer("❌ Couldn't find any sites at that amount. Try a different price or a bigger list.")
    await state.clear()


# --- START BOT ---
if __name__ == "__main__":
    print(f"Bot is starting... Owner: {WATERMARK}")
    asyncio.run(dp.start_polling(bot))