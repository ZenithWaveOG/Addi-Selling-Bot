import os
import logging
import random
import string
import asyncio
from datetime import datetime
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ========== CONFIGURATION ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8778422236"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")

from supabase import create_client, Client

PROD_199 = "199_per_100"
PROD_499 = "499_per_100"
PRODUCTS = {
    PROD_199: {"display": "199 per 100 off", "default_price": 199},
    PROD_499: {"display": "499 per 100 off", "default_price": 499},
}

CHANNEL_1 = "https://t.me/VIPAMMER"
CHANNEL_2 = "https://t.me/addiloots"
SUPPORT_BOT = "@ADDISUPPORT_BOT"

# Conversation states
(AWAITING_QUANTITY, AWAITING_PAYER_NAME, AWAITING_SCREENSHOT,
 ADMIN_ADD_COUPON_PRODUCT, ADMIN_ADD_COUPON_CODES,
 ADMIN_REMOVE_COUPON_PRODUCT, ADMIN_REMOVE_COUPON_NUMBER,
 ADMIN_CHANGE_PRICE_PRODUCT, ADMIN_CHANGE_PRICE_VALUE,
 ADMIN_BROADCAST_MESSAGE) = range(10)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
application = None

# ========== Helper Functions ==========
def generate_order_id() -> str:
    return "MNT-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def get_stock(product_key: str) -> int:
    result = supabase.table("coupon_codes") \
        .select("id", count="exact") \
        .eq("product_key", product_key) \
        .eq("is_used", False) \
        .execute()
    return result.count

async def get_payment_qr() -> Optional[str]:
    res = supabase.table("bot_config") \
        .select("value") \
        .eq("key", "payment_qr_file_id") \
        .execute()
    return res.data[0]["value"] if res.data else None

async def update_payment_qr(file_id: str):
    supabase.table("bot_config") \
        .upsert({"key": "payment_qr_file_id", "value": file_id}) \
        .execute()

async def broadcast_message(text: str):
    users = supabase.table("users").select("user_id").execute()
    for user in users.data:
        try:
            await application.bot.send_message(chat_id=user["user_id"], text=text)
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast failed: {e}")

async def get_last_10_buyers() -> str:
    orders = supabase.table("orders") \
        .select("user_id, product_key, quantity, total_amount, created_at, payer_name") \
        .eq("status", "completed") \
        .order("created_at", desc=True) \
        .limit(10) \
        .execute()
    if not orders.data:
        return "No completed orders yet."
    lines = []
    for idx, order in enumerate(orders.data, 1):
        user = supabase.table("users") \
            .select("first_name, username") \
            .eq("user_id", order["user_id"]) \
            .execute()
        name = user.data[0]["first_name"] if user.data else str(order["user_id"])
        prod = PRODUCTS.get(order["product_key"], {}).get("display", order["product_key"])
        dt = datetime.fromisoformat(order["created_at"]).strftime("%d %b %Y")
        lines.append(
            f"{idx}. {name} – {prod} x{order['quantity']} = ₹{order['total_amount']} "
            f"({order['payer_name']}) [{dt}]"
        )
    return "\n".join(lines)

async def register_user(user_id: int, username: str, first_name: str):
    existing = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "started_at": datetime.now().isoformat()
        }).execute()

async def send_coupon_codes(user_id: int, order_id: str, product_key: str, quantity: int) -> bool:
    codes_res = supabase.table("coupon_codes") \
        .select("id, code") \
        .eq("product_key", product_key) \
        .eq("is_used", False) \
        .order("id", asc=True) \
        .limit(quantity) \
        .execute()
    if len(codes_res.data) < quantity:
        return False
    code_ids = [c["id"] for c in codes_res.data]
    code_strings = [c["code"] for c in codes_res.data]
    supabase.table("coupon_codes") \
        .update({"is_used": True, "order_id": order_id}) \
        .in_("id", code_ids) \
        .execute()
    codes_text = "\n".join([f"<code>{c}</code>" for c in code_strings])
    msg = (
        "<b>✅ PAYMENT APPROVED ✅</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎉 Your coupon codes ({quantity} pcs):\n\n{codes_text}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Thank you for shopping with us!"
    )
    await application.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
    return True

async def get_product_price(product_key: str) -> int:
    res = supabase.table("bot_config") \
        .select("value") \
        .eq("key", f"price_{product_key}") \
        .execute()
    if res.data:
        return int(res.data[0]["value"])
    return PRODUCTS[product_key]["default_price"]

async def set_product_price(product_key: str, price: int):
    supabase.table("bot_config") \
        .upsert({"key": f"price_{product_key}", "value": str(price)}) \
        .execute()

# ========== User Menu Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username, user.first_name)

    keyboard = [
        ["🛒 Buy Coupon", "📊 Stock"],
        ["📦 My Orders", "📢 Our Channels"],
        ["🆘 Support"]
    ]
    if user.id == ADMIN_ID:
        keyboard.append(["👑 Admin Panel"])

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome_text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛍 WELCOME TO MYNTRA SHOP 🛍\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Professional Automated Delivery System.\n\n"
        "✅ Select an option:"
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Prevent menu interference during admin actions
    if context.user_data.get("admin_prod_key"):
        return

    text = update.message.text
    user_id = update.effective_user.id

    # HANDLE PAYER NAME
    if context.user_data.get("awaiting") == "payer_name":
        context.user_data["payer_name"] = text
        context.user_data["awaiting"] = "screenshot"
        await update.message.reply_text("📸 Now send payment screenshot.")
        return

    # HANDLE SCREENSHOT STEP (text fallback safety)
    if context.user_data.get("awaiting") == "screenshot":
        await update.message.reply_text("❌ Please send a photo, not text.")
        return

    if text == "🛒 Buy Coupon":
        keyboard = [
            [InlineKeyboardButton(f"{PRODUCTS[PROD_199]['display']} (₹{await get_product_price(PROD_199)})",
                                  callback_data=f"buy:{PROD_199}")],
            [InlineKeyboardButton(f"{PRODUCTS[PROD_499]['display']} (₹{await get_product_price(PROD_499)})",
                                  callback_data=f"buy:{PROD_499}")]
        ]
        await update.message.reply_text(
            "✅ Select A Coupon To Buy:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif text == "📊 Stock":
        stock1 = await get_stock(PROD_199)
        stock2 = await get_stock(PROD_499)
        msg = (
            "📊 <b>REAL-TIME STOCK</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 {PRODUCTS[PROD_199]['display']}: <b>{stock1}</b>\n"
            f"🔹 {PRODUCTS[PROD_499]['display']}: <b>{stock2}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    elif text == "📦 My Orders":
        orders = supabase.table("orders") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "completed") \
            .order("created_at", desc=True) \
            .execute()
        if not orders.data:
            await update.message.reply_text("❌ No completed orders found.")
            return
        for order in orders.data:
            codes_res = supabase.table("coupon_codes") \
                .select("code") \
                .eq("order_id", order["order_id"]) \
                .execute()
            codes_str = "\n".join([f"<code>{c['code']}</code>" for c in codes_res.data])
            prod_name = PRODUCTS.get(order["product_key"], {}).get("display", order["product_key"])
            dt = datetime.fromisoformat(order["created_at"]).strftime("%d %b %Y, %I:%M %p")
            msg = (
                f"<b>📦 Order #{order['order_id']}</b>\n"
                f"📅 {dt}\n"
                f"🛍 {prod_name} x{order['quantity']} = ₹{order['total_amount']}\n"
                f"🎫 Codes:\n{codes_str}\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )
            await update.message.reply_text(msg, parse_mode="HTML")

    elif text == "📢 Our Channels":
        keyboard = [
            [InlineKeyboardButton("🔹 VIP AMMER", url=CHANNEL_1)],
            [InlineKeyboardButton("🔹 ADDI LOOTS", url=CHANNEL_2)]
        ]
        await update.message.reply_text(
            "📢 Join our official channels for updates:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif text == "🆘 Support":
        await update.message.reply_text(f"🆘 Support: {SUPPORT_BOT}", parse_mode="HTML")

    elif text == "👑 Admin Panel" and user_id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("➕ Add Coupon", callback_data="admin_add_coupon")],
            [InlineKeyboardButton("➖ Remove Coupon", callback_data="admin_remove_coupon")],
            [InlineKeyboardButton("💰 Change Prices", callback_data="admin_change_price")],
            [InlineKeyboardButton("🖼 Update QR", callback_data="admin_update_qr")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
            [InlineKeyboardButton("📋 Last 10 Buyers", callback_data="admin_last10")]
        ]
        await update.message.reply_text(
            "👑 Admin Panel – choose action:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ========== Buy Flow ==========
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_key = query.data.split(":")[1]
    context.user_data["buy_product"] = product_key
    stock = await get_stock(product_key)
    await query.edit_message_text(
        f"🏷️ Product: {PRODUCTS[product_key]['display']}\n"
        f"📦 Available stock: {stock}\n\n"
        f"📋 Send the number of coupons you want to buy:"
    )
    return AWAITING_QUANTITY

async def quantity_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        qty = int(update.message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a valid positive integer.")
        return AWAITING_QUANTITY

    product_key = context.user_data.get("buy_product")
    if not product_key:
        await update.message.reply_text("Session expired. Start over with /start")
        return ConversationHandler.END

    stock = await get_stock(product_key)
    if qty > stock:
        await update.message.reply_text(
            f"❌ Only {stock} left in that option. Please select a lower quantity."
        )
        return AWAITING_QUANTITY

    price = await get_product_price(product_key)
    total = price * qty
    order_id = generate_order_id()

    supabase.table("orders").insert({
        "order_id": order_id,
        "user_id": user.id,
        "product_key": product_key,
        "quantity": qty,
        "total_amount": total,
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }).execute()

    context.user_data["pending_order"] = {
        "order_id": order_id,
        "product_key": product_key,
        "quantity": qty,
        "total": total
    }

    qr_file_id = await get_payment_qr()
    if not qr_file_id:
        await update.message.reply_text("⚠️ Payment QR not configured by admin. Please try later.")
        return ConversationHandler.END

    caption = (
        f"<b>🧾 INVOICE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order ID: <code>{order_id}</code>\n"
        f"📦 Product: {PRODUCTS[product_key]['display']}\n"
        f"💰 Pay Exactly: ₹{total}\n"
        f"⏳ QR valid for 10 minutes. Session auto-resets after.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid:{order_id}")]])
    await update.message.reply_photo(photo=qr_file_id, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    return ConversationHandler.END

async def paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.split(":")[1]
    context.user_data["paid_order_id"] = order_id
    context.user_data["awaiting"] = "payer_name"

    await query.message.reply_text("🧾 Send payer name (UPI name):")

async def payer_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payer_name = update.message.text.strip()
    context.user_data["payer_name"] = payer_name
    await update.message.reply_text("Now please send the payment screenshot (photo).")
    return AWAITING_SCREENSHOT

async def screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.user_data.get("awaiting") != "screenshot":
            return

        user = update.effective_user
        order_id = context.user_data.get("paid_order_id")
        payer_name = context.user_data.get("payer_name")

        if not order_id or not payer_name:
            await update.message.reply_text("❌ Session expired.")
            return

        photo = update.message.photo[-1]

        # Save to DB
        supabase.table("orders") \
            .update({
                "payer_name": payer_name,
                "screenshot_file_id": photo.file_id
            }) \
            .eq("order_id", order_id) \
            .execute()

        await update.message.reply_text("⏳ Payment sent to admin for verification.")

        # Get order
        order = supabase.table("orders") \
            .select("*") \
            .eq("order_id", order_id) \
            .single() \
            .execute()

        prod = PRODUCTS.get(order.data["product_key"], {}).get("display", "Unknown")

        # Send to admin
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"admin_accept:{order_id}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"admin_decline:{order_id}")
            ]
        ])

        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo.file_id,
            caption=(
                f"📩 NEW PAYMENT\n\n"
                f"User: {user.first_name}\n"
                f"Order: {order_id}\n"
                f"Product: {prod}\n"
                f"Amount: ₹{order.data['total_amount']}\n"
                f"Payer: {payer_name}"
            ),
            reply_markup=keyboard
        )

        # Clear session
        context.user_data.clear()

    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        await update.message.reply_text("❌ Error processing payment.")
        
# ========== Admin Actions ==========
async def admin_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()

        order_id = query.data.split(":")[1]

        order = supabase.table("orders") \
            .select("*") \
            .eq("order_id", order_id) \
            .single() \
            .execute()

        if not order.data:
            await query.edit_message_caption("❌ Order not found.")
            return

        if order.data["status"] != "pending":
            await query.edit_message_caption(f"⚠️ Already {order.data['status']}")
            return

        product_key = order.data["product_key"]
        quantity = order.data["quantity"]
        user_id = order.data["user_id"]

        # Check stock
        stock = await get_stock(product_key)
        if stock < quantity:
            await query.edit_message_caption("❌ Not enough stock.")
            return

        # Get codes
        codes_res = supabase.table("coupon_codes") \
            .select("id, code") \
            .eq("product_key", product_key) \
            .eq("is_used", False) \
            .limit(quantity) \
            .execute()

        if len(codes_res.data) < quantity:
            await query.edit_message_caption("❌ Not enough coupons available.")
            return

        codes = [c["code"] for c in codes_res.data]
        ids = [c["id"] for c in codes_res.data]

        # Mark used
        supabase.table("coupon_codes") \
            .update({"is_used": True, "order_id": order_id}) \
            .in_("id", ids) \
            .execute()

        # Send to user
        codes_text = "\n".join(codes)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Payment Approved!\n\nYour Codes:\n{codes_text}"
        )

        # Update order
        supabase.table("orders") \
            .update({"status": "completed"}) \
            .eq("order_id", order_id) \
            .execute()

        await query.edit_message_caption(f"✅ Order {order_id} approved")

    except Exception as e:
        logger.error(f"Accept error: {e}")
        await query.edit_message_caption("❌ Error while approving order.")

async def admin_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":")[1]
    supabase.table("orders").update({"status": "declined"}).eq("order_id", order_id).execute()

    order = supabase.table("orders").select("user_id").eq("order_id", order_id).single().execute()
    if order.data:
        await context.bot.send_message(
            chat_id=order.data["user_id"],
            text="❌ Your order has been declined by admin. If you have any issue, please contact support."
        )
    await query.edit_message_caption(f"❌ Order {order_id} declined.")

# ========== Admin Panel Handlers ==========
async def admin_add_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(PRODUCTS[PROD_199]['display'],
         callback_data=f"add_coupon_prod_{PROD_199}")],
        [InlineKeyboardButton(PRODUCTS[PROD_499]['display'],
         callback_data=f"add_coupon_prod_{PROD_499}")]
    ]

    await query.edit_message_text(
        "Select product to add coupons:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_ADD_COUPON_PRODUCT

async def admin_add_coupon_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("add_coupon_prod_", "")
    context.user_data["admin_prod_key"] = prod_key
    await query.edit_message_text(
        f"Send the coupon codes for {PRODUCTS[prod_key]['display']}.\n"
        "Send one code per line or separate by newline.\n"
        "Example:\nCODE123\nCODE456\nCODE789"
    )
    return ADMIN_ADD_COUPON_CODES

async def admin_add_coupon_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        prod_key = context.user_data.get("admin_prod_key")
        if not prod_key:
            await update.message.reply_text("Session expired.")
            return ConversationHandler.END

        codes_text = update.message.text.strip()
        codes = [line.strip() for line in codes_text.splitlines() if line.strip()]

        if not codes:
            await update.message.reply_text("No valid codes found.")
            return ADMIN_ADD_COUPON_CODES

        inserted = 0
        for code in codes:
            try:
                supabase.table("coupon_codes").insert({
                    "product_key": prod_key,
                    "code": code,
                    "is_used": False
                }).execute()
                inserted += 1
            except Exception as e:
                logger.error(f"Insert error: {e}")

        await update.message.reply_text(f"✅ Added {inserted} coupon(s).")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Add coupon error: {e}")
        await update.message.reply_text("❌ Error occurred. Try again.")
        return ConversationHandler.END

async def admin_remove_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(PRODUCTS[PROD_199]['display'],
         callback_data=f"remove_coupon_prod_{PROD_199}")],
        [InlineKeyboardButton(PRODUCTS[PROD_499]['display'],
         callback_data=f"remove_coupon_prod_{PROD_499}")]
    ]

    await query.edit_message_text(
        "Select product to remove coupons:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_REMOVE_COUPON_PRODUCT

async def admin_remove_coupon_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("remove_coupon_prod_", "")
    context.user_data["admin_prod_key"] = prod_key
    stock = await get_stock(prod_key)
    await query.edit_message_text(
        f"Current stock for {PRODUCTS[prod_key]['display']}: {stock}\n"
        f"Send the number of coupons to REMOVE (oldest first):"
    )
    return ADMIN_REMOVE_COUPON_NUMBER

async def admin_remove_coupon_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        prod_key = context.user_data.get("admin_prod_key")
        if not prod_key:
            await update.message.reply_text("❌ Session expired. Start again.")
            return ConversationHandler.END

        if not text or not text.strip().isdigit():
            await update.message.reply_text("❌ Send a valid number.")
            return ADMIN_REMOVE_COUPON_NUMBER

        num = int(text.strip())
        stock = await get_stock(prod_key)
        if num > stock:
            await update.message.reply_text(f"❌ Only {stock} available.")
            return ADMIN_REMOVE_COUPON_NUMBER

        codes_res = supabase.table("coupon_codes") \
            .select("id") \
            .eq("product_key", prod_key) \
            .eq("is_used", False) \
            .limit(num) \
            .execute()

        if not codes_res.data:
            await update.message.reply_text("❌ No coupons found.")
            return ConversationHandler.END

        ids = [c["id"] for c in codes_res.data]
        supabase.table("coupon_codes").delete().in_("id", ids).execute()

        await update.message.reply_text(f"✅ Removed {len(ids)} coupon(s).")
        context.user_data.pop("admin_prod_key", None)
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Remove error: {e}")
        await update.message.reply_text("❌ Internal error. Try again.")

async def admin_change_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(PRODUCTS[PROD_199]['display'],
         callback_data=f"chprice_prod_{PROD_199}")],
        [InlineKeyboardButton(PRODUCTS[PROD_499]['display'],
         callback_data=f"chprice_prod_{PROD_499}")]
    ]

    await query.edit_message_text(
        "Select product to change price:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_CHANGE_PRICE_PRODUCT

async def admin_change_price_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("chprice_prod_", "")
    context.user_data["admin_prod_key"] = prod_key
    current = await get_product_price(prod_key)
    await query.edit_message_text(
        f"Current price for {PRODUCTS[prod_key]['display']}: ₹{current}\n"
        f"Send the new price (integer):"
    )
    return ADMIN_CHANGE_PRICE_VALUE

async def admin_change_price_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prod_key = context.user_data.get("admin_prod_key")
    if not prod_key:
        await update.message.reply_text("Session expired.")
        return ConversationHandler.END
    try:
        new_price = int(update.message.text.strip())
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Send a positive integer.")
        return ADMIN_CHANGE_PRICE_VALUE

    await set_product_price(prod_key, new_price)
    await update.message.reply_text(f"✅ Price for {PRODUCTS[prod_key]['display']} updated to ₹{new_price}.")
    return ConversationHandler.END

async def admin_update_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please send the new payment QR code as a photo.")

async def admin_photo_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    photo = update.message.photo[-1]
    await update_payment_qr(photo.file_id)
    await update.message.reply_text("✅ Payment QR code updated successfully!")

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Send the broadcast message (text only):")
    return ADMIN_BROADCAST_MESSAGE

async def admin_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text
    await broadcast_message(text)
    await update.message.reply_text("✅ Broadcast sent to all users.")
    return ConversationHandler.END

async def admin_last10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    report = await get_last_10_buyers()
    await query.edit_message_text(f"<b>Last 10 Buyers</b>\n\n{report}", parse_mode="HTML")

# ========== Main ==========
def main():
    global application
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handlers
    buy_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_callback, pattern="^buy:")],
        states={AWAITING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_received)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    add_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_coupon_start, pattern="^admin_add_coupon$")],
        states={
            ADMIN_ADD_COUPON_PRODUCT: [CallbackQueryHandler(admin_add_coupon_product, pattern="^add_coupon_prod_")],
            ADMIN_ADD_COUPON_CODES: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_coupon_codes)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    remove_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_remove_coupon_start, pattern="^admin_remove_coupon$")],
        states={
            ADMIN_REMOVE_COUPON_PRODUCT: [CallbackQueryHandler(admin_remove_coupon_product, pattern="^remove_coupon_prod_")],
            ADMIN_REMOVE_COUPON_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_coupon_number)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    price_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_change_price_start, pattern="^admin_change_price$")],
        states={
            ADMIN_CHANGE_PRICE_PRODUCT: [CallbackQueryHandler(admin_change_price_product, pattern="^chprice_prod_")],
            ADMIN_CHANGE_PRICE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_change_price_value)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    broadcast_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$")],
        states={ADMIN_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_message)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    # Add all handlers (order matters)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(paid_callback, pattern="^paid:"))
    application.add_handler(CallbackQueryHandler(admin_accept, pattern="^admin_accept:"))
    application.add_handler(CallbackQueryHandler(admin_decline, pattern="^admin_decline:"))
    application.add_handler(CallbackQueryHandler(admin_update_qr, pattern="^admin_update_qr$"))
    application.add_handler(CallbackQueryHandler(admin_last10, pattern="^admin_last10$"))
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), admin_photo_qr))

    # Conversation handlers
    application.add_handler(buy_handler)
    application.add_handler(add_handler)
    application.add_handler(remove_handler)
    application.add_handler(price_handler)
    application.add_handler(broadcast_handler)

    # Generic handlers (must be last)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    application.add_handler(MessageHandler(filters.PHOTO, screenshot_received))

    # Webhook setup
    port = int(os.environ.get("PORT", 10000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,
        webhook_url=f"{webhook_url}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
