import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from enum import Enum
import hashlib
from functools import wraps

import requests
import pymongo
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ========== কনফিগারেশন ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # যেমন: @daraz_offers
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/daraz_offers")  # চ্যানেলের লিঙ্ক
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]

# চেক করার ইন্টারভাল
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))  # মিনিট
MIN_DISCOUNT_FOR_CHANNEL = int(os.getenv("MIN_DISCOUNT", "10"))  # চ্যানেলে পোস্টের জন্য ন্যূনতম ডিসকাউন্ট

# ========== লগিং ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== MongoDB সেটআপ ==========
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client['daraz_mandatory_bot']
    
    # কালেকশনসমূহ
    users_collection = db['users']  # ইউজার তথ্য (চ্যানেল জয়েন স্ট্যাটাস সহ)
    products_collection = db['products']  # সব প্রোডাক্ট
    user_tracks_collection = db['user_tracks']  # ইউজারদের ট্র্যাক
    channel_posts_collection = db['channel_posts']  # চ্যানেলে পোস্ট করা অফার
    stats_collection = db['stats']  # পরিসংখ্যান
    
    # ইনডেক্স
    users_collection.create_index("user_id", unique=True)
    users_collection.create_index("channel_joined")
    products_collection.create_index("url", unique=True)
    user_tracks_collection.create_index([("user_id", 1), ("url", 1)], unique=True)
    
    logger.info("✅ MongoDB সংযোগ সফল")
except Exception as e:
    logger.error(f"❌ MongoDB সংযোগ ব্যর্থ: {e}")
    raise

# ========== ডেকোরেটর: চ্যানেল চেক ==========
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """ইউজার চ্যানেলে জয়েন করেছে কিনা চেক করে"""
    try:
        # প্রথমে ডাটাবেসে চেক
        user = users_collection.find_one({"user_id": user_id})
        if user and user.get("channel_joined") == True:
            # ৭ দিন পর পর রিফ্রেশ করুন
            last_check = user.get("last_channel_check", datetime.min)
            if datetime.utcnow() - last_check < timedelta(days=7):
                return True
        
        # টেলিগ্রাম API দিয়ে চেক
        chat_member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        
        is_member = chat_member.status in ['member', 'administrator', 'creator']
        
        # ডাটাবেস আপডেট
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "channel_joined": is_member,
                    "last_channel_check": datetime.utcnow()
                }
            },
            upsert=True
        )
        
        return is_member
        
    except Exception as e:
        logger.error(f"চ্যানেল মেম্বারশিপ চেক করতে সমস্যা: {e}")
        # এরর হলে False রিটার্ন করবেন না, বরং সতর্কতা সহ True দিন
        # কারণ টেলিগ্রাম API মাঝে মাঝে এরর দিতে পারে
        logger.warning("চ্যানেল চেক করতে সমস্যা, অ্যাক্সেস দেয়া হচ্ছে")
        return True

def require_channel(func):
    """ডেকোরেটর - ফাংশন ব্যবহারের আগে চ্যানেল জয়েন চেক করে"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        
        # চ্যানেল মেম্বারশিপ চেক
        is_member = await check_channel_membership(user_id, context)
        
        if not is_member:
            # চ্যানেল জয়েনের মেসেজ
            msg = (
                "⚠️ <b>চ্যানেল জয়েন করা আবশ্যক!</b>\n\n"
                f"আমাদের চ্যানেল <b>{CHANNEL_ID}</b> এ জয়েন করুন, তারপর আবার চেষ্টা করুন।\n\n"
                "🔰 চ্যানেল জয়েন করলে /start দিয়ে আবার শুরু করুন।"
            )
            
            keyboard = [
                [InlineKeyboardButton("📢 চ্যানেল জয়েন করুন", url=CHANNEL_LINK)],
                [InlineKeyboardButton("✅ জয়েন করেছি", callback_data="check_join")]
            ]
            
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    msg, 
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text(
                    msg, 
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return
        
        # চ্যানেল জয়েন করলে মূল ফাংশন কল
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========== ডাটাক্লাস ==========
@dataclass
class ProductInfo:
    """প্রোডাক্ট তথ্য"""
    url: str
    title: str
    current_price: float
    original_price: Optional[float] = None
    discount: int = 0
    image_url: Optional[str] = None
    rating: Optional[float] = None
    sold_count: Optional[int] = None
    
    @property
    def discount_percentage(self) -> int:
        if self.original_price and self.original_price > self.current_price:
            return round(((self.original_price - self.current_price) / self.original_price) * 100)
        return 0
    
    @property
    def product_hash(self) -> str:
        """প্রোডাক্ট ইউনিক আইডি"""
        return hashlib.md5(self.url.encode()).hexdigest()[:10]

# ========== ডারাজ স্ক্র্যাপার ==========
class DarazScraper:
    """ডারাজ থেকে প্রোডাক্ট তথ্য সংগ্রহ"""
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
    }
    
        @classmethod
    async def get_product_info(cls, url: str) -> Optional[ProductInfo]:
        """প্রোডাক্টের তথ্য সংগ্রহ"""
        try:
            response = await asyncio.to_thread(
                requests.get, url, headers=cls.HEADERS, timeout=15
            )
            response.raise_for_status()
            
            import json, re
            # জাভাস্ক্রিপ্ট ডাটা থেকে তথ্য বের করা
            match = re.search(r'app\.run\(({.*?})\);', response.text)
            if not match:
                return None
                
            data = json.loads(match.group(1))
            product_data = data.get('data', {}).get('root', {}).get('fields', {})
            sku_data = product_data.get('skuInfos', {}).get('0', {})

            title = product_data.get('product', {}).get('title')
            current_price = float(sku_data.get('price', {}).get('salePrice', 0))
            original_price = float(sku_data.get('price', {}).get('originalPrice', 0))
            image = sku_data.get('image')

            if not title or not current_price:
                return None

            return ProductInfo(
                url=url,
                title=title,
                current_price=current_price,
                original_price=original_price if original_price > 0 else None,
                image_url=image
            )

        except Exception as e:
            logger.error(f"স্ক্র্যাপিং এ সমস্যা: {e}")
            return None

            
        except Exception as e:
            logger.error(f"স্ক্র্যাপিং এ সমস্যা: {e}")
            return None
    
    @staticmethod
    def _extract_title(soup):
        selectors = [
            "h1.pdp-mod-product-badge-title",
            "h1.pdp-product-title",
            "[data-testid='product-title']"
        ]
        for sel in selectors:
            tag = soup.select_one(sel)
            if tag:
                return tag.text.strip()[:150]
        return None
    
    @staticmethod
    def _extract_prices(soup):
        current = None
        original = None
        
        price_selectors = [
            "span.pdp-price",
            ".pdp-product-price span",
            "[data-testid='price']"
        ]
        for sel in price_selectors:
            tag = soup.select_one(sel)
            if tag:
                try:
                    text = tag.text.strip()
                    current = float(text.replace('৳', '').replace(',', '').replace('BDT', '').strip())
                    break
                except:
                    continue
        
        original_selectors = [
            "span.pdp-price-original",
            ".original-price",
            "[data-testid='original-price']"
        ]
        for sel in original_selectors:
            tag = soup.select_one(sel)
            if tag:
                try:
                    original = float(tag.text.replace('৳', '').replace(',', '').strip())
                    break
                except:
                    continue
        
        return current, original
    
    @staticmethod
    def _extract_image(soup):
        selectors = [
            "img.pdp-mod-common-image",
            ".gallery-preview-panel img",
            "[data-testid='product-image']"
        ]
        for sel in selectors:
            img = soup.select_one(sel)
            if img and img.get('src'):
                url = img['src']
                if url.startswith('//'):
                    url = 'https:' + url
                return url
        return None
    
    @staticmethod
    def _extract_rating(soup):
        selectors = [".pdp-review-summary-average", "[data-testid='rating']"]
        for sel in selectors:
            tag = soup.select_one(sel)
            if tag:
                try:
                    return float(tag.text.strip())
                except:
                    continue
        return None
    
    @staticmethod
    def _extract_sold_count(soup):
        selectors = [".sale-count", "[data-testid='sold']"]
        for sel in selectors:
            tag = soup.select_one(sel)
            if tag:
                text = tag.text.strip()
                nums = ''.join(filter(str.isdigit, text))
                return int(nums) if nums else None
        return None

# ========== নোটিফিকেশন ম্যানেজার ==========
class NotificationManager:
    """ইউজার ও চ্যানেলের জন্য নোটিফিকেশন"""
    
    @staticmethod
    async def notify_users(context: ContextTypes.DEFAULT_TYPE, product: ProductInfo):
        """যেসব ইউজার প্রোডাক্ট ট্র্যাক করছে তাদের নোটিফাই করা"""
        tracks = user_tracks_collection.find({
            "url": product.url,
            "notified": False,
            "target_price": {"$gte": product.current_price}
        })
        
        notified_count = 0
        for track in tracks:
            try:
                # ইউজার চ্যানেলে আছে কিনা চেক করুন (নোটিফিকেশন দেওয়ার আগেও)
                is_member = await check_channel_membership(track['user_id'], context)
                if not is_member:
                    continue  # চ্যানেলে না থাকলে নোটিফিকেশন দেবেন না
                
                message = (
                    f"🎉 <b>আপনার ট্র্যাক করা প্রোডাক্টের দাম কমেছে!</b>\n\n"
                    f"📦 <b>{product.title}</b>\n"
                    f"💰 <b>বর্তমান দাম:</b> ৳{product.current_price:,.2f}\n"
                    f"📉 <b>ডিসকাউন্ট:</b> {product.discount_percentage}%\n"
                    f"🎯 <b>আপনার টার্গেট:</b> ৳{track['target_price']:,.2f}\n\n"
                    f"👉 <a href='{product.url}'>প্রোডাক্ট দেখুন</a>"
                )
                
                await context.bot.send_message(
                    chat_id=track['user_id'],
                    text=message,
                    parse_mode=ParseMode.HTML
                )
                
                user_tracks_collection.update_one(
                    {"_id": track["_id"]},
                    {"$set": {"notified": True, "notified_at": datetime.utcnow()}}
                )
                
                notified_count += 1
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"ইউজার নোটিফিকেশন ব্যর্থ: {e}")
        
        if notified_count:
            logger.info(f"✅ {notified_count} জন ইউজারকে নোটিফাই করা হয়েছে")
    
    @staticmethod
    async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, product: ProductInfo):
        """চ্যানেলে অফার পোস্ট করা"""
        
        already_posted = channel_posts_collection.find_one({
            "product_url": product.url,
            "posted_date": {"$gte": datetime.utcnow() - timedelta(days=7)}
        })
        
        if already_posted:
            return
        
        message = NotificationManager._create_channel_post(product)
        
        try:
            if product.image_url:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=product.image_url,
                    caption=message,
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
            
            channel_posts_collection.insert_one({
                "product_url": product.url,
                "product_title": product.title,
                "price": product.current_price,
                "discount": product.discount_percentage,
                "posted_date": datetime.utcnow()
            })
            
            stats_collection.update_one(
                {"type": "channel_posts"},
                {"$inc": {"count": 1}},
                upsert=True
            )
            
            logger.info(f"✅ চ্যানেলে পোস্ট করা হয়েছে: {product.title[:50]}")
            
        except Exception as e:
            logger.error(f"চ্যানেল পোস্ট ব্যর্থ: {e}")
    
    @staticmethod
    def _create_channel_post(product: ProductInfo) -> str:
        """চ্যানেলের জন্য পোস্ট তৈরি"""
        
        if product.discount_percentage >= 50:
            header = "🔥 <b>বাম্পার অফার!</b> 🔥"
        elif product.discount_percentage >= 30:
            header = "⚡️ <b>সুপার ডিল!</b> ⚡️"
        else:
            header = "🛍️ <b>নতুন অফার!</b>"
        
        message = f"{header}\n\n"
        message += f"<b>📦 {product.title}</b>\n\n"
        
        if product.original_price and product.original_price > product.current_price:
            message += f"<b>💵 মূল দাম:</b> <s>৳{product.original_price:,.2f}</s>\n"
            message += f"<b>💰 বর্তমান দাম:</b> <code>৳{product.current_price:,.2f}</code>\n"
            message += f"<b>📉 ডিসকাউন্ট:</b> <code>{product.discount_percentage}%</code>\n"
        else:
            message += f"<b>💰 দাম:</b> <code>৳{product.current_price:,.2f}</code>\n"
        
        if product.rating:
            message += f"<b>⭐️ রেটিং:</b> {product.rating}/5\n"
        if product.sold_count:
            message += f"<b>👥 বিক্রি হয়েছে:</b> {product.sold_count}+\n"
        
        message += f"\n🔗 <a href='{product.url}'>দারাজে দেখুন</a>\n\n"
        message += "<i>#daraz #offer #বাংলাদেশ #শপিং</i>"
        
        return message

# ========== ব্যাকগ্রাউন্ড জব ==========
async def price_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """প্রাইস মনিটরিং"""
    logger.info("🔄 প্রাইস চেক শুরু...")
    
    try:
        products = list(products_collection.find({}))
        
        if not products:
            logger.info("কোনো প্রোডাক্ট নেই")
            return
        
        for product_data in products:
            try:
                scraper = DarazScraper()
                current_info = await scraper.get_product_info(product_data['url'])
                
                if not current_info:
                    continue
                
                old_price = product_data.get('last_price')
                old_discount = product_data.get('last_discount', 0)
                
                price_decreased = old_price and current_info.current_price < old_price
                discount_increased = current_info.discount_percentage > old_discount
                
                products_collection.update_one(
                    {"_id": product_data["_id"]},
                    {
                        "$set": {
                            "current_price": current_info.current_price,
                            "original_price": current_info.original_price,
                            "discount": current_info.discount_percentage,
                            "last_checked": datetime.utcnow(),
                            "last_price": current_info.current_price,
                            "last_discount": current_info.discount_percentage,
                            "title": current_info.title
                        },
                        "$inc": {"check_count": 1}
                    }
                )
                
                if price_decreased or discount_increased:
                    logger.info(f"📉 দাম কমেছে: {current_info.title[:50]}")
                    
                    # ইউজারদের নোটিফাই
                    await NotificationManager.notify_users(context, current_info)
                    
                    # চ্যানেলে পোস্ট
                    if current_info.discount_percentage >= MIN_DISCOUNT_FOR_CHANNEL:
                        await NotificationManager.post_to_channel(context, current_info)
                
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"প্রোডাক্ট চেক করতে সমস্যা: {e}")
                continue
        
        logger.info("✅ প্রাইস চেক শেষ")
        
    except Exception as e:
        logger.error(f"মনিটরিং জবে সমস্যা: {e}")

# ========== ইউজার কমান্ড (চ্যানেল চেক সহ) ==========
@require_channel
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """স্টার্ট কমান্ড"""
    user = update.effective_user
    
    # ইউজার আপডেট
    users_collection.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_active": datetime.utcnow(),
                "channel_joined": True
            }
        },
        upsert=True
    )
    
    welcome_msg = (
        f"👋 <b>স্বাগতম {user.first_name}!</b>\n\n"
        f"আপনি সফলভাবে চ্যানেলে জয়েন করেছেন! 🎉\n\n"
        f"আমি <b>দারাজ হাইব্রিড ট্র্যাকার বট</b>। আমি দুইটি কাজ করি:\n\n"
        f"1️⃣ <b>পার্সোনাল ট্র্যাকিং:</b> আপনি প্রোডাক্ট লিঙ্ক ও টার্গেট প্রাইস দিন, দাম কমলে জানাব\n"
        f"2️⃣ <b>চ্যানেল অটো-পোস্ট:</b> ভালো অফার পেলে চ্যানেলে পোস্ট করি\n\n"
        f"📢 <b>আমাদের চ্যানেল:</b> {CHANNEL_ID}\n\n"
        f"📌 <b>ব্যবহার করবেন যেভাবে:</b>\n"
        f"প্রোডাক্টের লিঙ্ক ও আপনার টার্গেট প্রাইস দিন\n"
        f"যেমন: <code>https://daraz.com.bd/product... 1500</code>"
    )
    
    keyboard = [
        [InlineKeyboardButton("📢 চ্যানেল ভিজিট", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📋 আমার ট্র্যাক", callback_data="my_tracks"),
         InlineKeyboardButton("ℹ️ সাহায্য", callback_data="help")]
    ]
    
    await update.message.reply_text(
        welcome_msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@require_channel
async def handle_track_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ইউজারের ট্র্যাক রিকোয়েস্ট"""
    try:
        text = update.message.text.strip()
        parts = text.split()
        
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ ভুল ফরম্যাট!\n"
                "সঠিক ফরম্যাট: `<লিঙ্ক> <টার্গেট প্রাইস>`\n"
                "উদাহরণ: `https://daraz.com.bd/product... 1500`"
            )
            return
        
        url = parts[0]
        try:
            target_price = float(parts[1])
        except:
            await update.message.reply_text("❌ টার্গেট প্রাইস সঠিক সংখ্যা দিন!")
            return
        
        user_id = update.effective_user.id
        
        if 'daraz.com.bd' not in url and 'daraz.com' not in url:
            await update.message.reply_text("❌ শুধু দারাজের লিঙ্ক দিন!")
            return
        
        status_msg = await update.message.reply_text("⏳ প্রোডাক্ট তথ্য সংগ্রহ করা হচ্ছে...")
        
        scraper = DarazScraper()
        product_info = await scraper.get_product_info(url)
        
        if not product_info:
            await status_msg.edit_text("❌ প্রোডাক্ট তথ্য পাওয়া যায়নি!")
            return
        
        # প্রোডাক্ট মাস্টার লিস্টে যোগ
        products_collection.update_one(
            {"url": url},
            {
                "$set": {
                    "url": url,
                    "title": product_info.title,
                    "current_price": product_info.current_price,
                    "original_price": product_info.original_price,
                    "discount": product_info.discount_percentage,
                    "last_checked": datetime.utcnow(),
                    "last_price": product_info.current_price,
                    "last_discount": product_info.discount_percentage
                },
                "$setOnInsert": {
                    "first_seen": datetime.utcnow()
                }
            },
            upsert=True
        )
        
        # ইউজার ট্র্যাক যোগ
        try:
            user_tracks_collection.insert_one({
                "user_id": user_id,
                "url": url,
                "target_price": target_price,
                "notified": False,
                "created_at": datetime.utcnow(),
                "product_title": product_info.title,
                "current_price_at_track": product_info.current_price
            })
            
            msg = (
                f"✅ <b>ট্র্যাকিং সেট হয়েছে!</b>\n\n"
                f"📦 <b>{product_info.title}</b>\n"
                f"💰 বর্তমান দাম: ৳{product_info.current_price:,.2f}\n"
                f"🎯 আপনার টার্গেট: ৳{target_price:,.2f}\n"
                f"📉 ডিসকাউন্ট: {product_info.discount_percentage}%\n\n"
                f"যখন দাম ৳{target_price:,.2f} বা এর নিচে নামবে, আমি আপনাকে জানাব!"
            )
            
            keyboard = [
                [InlineKeyboardButton("📋 আমার ট্র্যাক লিস্ট", callback_data="my_tracks")],
                [InlineKeyboardButton("📢 চ্যানেল দেখুন", url=CHANNEL_LINK)]
            ]
            
            await status_msg.edit_text(
                msg, 
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except pymongo.errors.DuplicateKeyError:
            await status_msg.edit_text(
                "⚠️ আপনি এই প্রোডাক্টটি আগেই ট্র্যাক করছেন!\n"
                "আপনার ট্র্যাক লিস্ট দেখতে /mytracks ব্যবহার করুন।"
            )
        
    except Exception as e:
        logger.error(f"ট্র্যাক হ্যান্ডলিং এ সমস্যা: {e}")
        await update.message.reply_text("❌ সমস্যা হয়েছে! আবার চেষ্টা করুন।")

@require_channel
async def my_tracks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ইউজারের ট্র্যাক লিস্ট"""
    user_id = update.effective_user.id
    
    tracks = list(user_tracks_collection.find(
        {"user_id": user_id}
    ).sort("created_at", -1).limit(20))
    
    if not tracks:
        await update.message.reply_text(
            "📭 আপনি এখনো কোনো প্রোডাক্ট ট্র্যাক করছেন না।\n"
            "একটি দারাজ লিঙ্ক ও টার্গেট প্রাইস দিন শুরু করতে।"
        )
        return
    
    msg = "<b>📋 আপনার ট্র্যাক করা প্রোডাক্ট:</b>\n\n"
    
    for i, track in enumerate(tracks, 1):
        status = "✅ পৌঁছেছে" if track.get('notified') else "⏳ অপেক্ষমাণ"
        msg += f"{i}. <b>{track.get('product_title', 'Unknown')[:50]}</b>\n"
        msg += f"   🎯 টার্গেট: ৳{track['target_price']:,.2f} | {status}\n"
        msg += f"   🔗 <a href='{track['url']}'>লিঙ্ক</a>\n\n"
    
    keyboard = [
        [InlineKeyboardButton("🗑 সব মুছুন", callback_data="clear_my_tracks")],
        [InlineKeyboardButton("📢 চ্যানেল দেখুন", url=CHANNEL_LINK)]
    ]
    
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

# ========== কলব্যাক হ্যান্ডলার ==========
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """বাটন কলব্যাক"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        user_id = update.effective_user.id
        is_member = await check_channel_membership(user_id, context)
        
        if is_member:
            await query.edit_message_text(
                "✅ আপনি চ্যানেলে জয়েন করেছেন! এখন /start দিন আবার।",
                parse_mode=ParseMode.HTML
            )
        else:
            await query.edit_message_text(
                "❌ আপনি এখনও চ্যানেলে জয়েন করেননি। দয়া করে জয়েন করুন।",
                parse_mode=ParseMode.HTML
            )
    
    elif query.data == "my_tracks":
        await my_tracks_command(update, context)
    
    elif query.data == "clear_my_tracks":
        user_id = update.effective_user.id
        result = user_tracks_collection.delete_many({"user_id": user_id})
        await query.edit_message_text(f"✅ {result.deleted_count} টি ট্র্যাক মুছে ফেলা হয়েছে।")
    
    elif query.data == "help":
        help_text = (
            "<b>ℹ️ সাহায্য ও নির্দেশিকা</b>\n\n"
            "<b>🔹 প্রোডাক্ট ট্র্যাক করবেন যেভাবে:</b>\n"
            "দারাজ লিঙ্ক ও টার্গেট প্রাইস দিন\n"
            "উদাহরণ: <code>https://daraz.com.bd/product... 1500</code>\n\n"
            "<b>🔹 কমান্ড সমূহ:</b>\n"
            "/start - বট শুরু\n"
            "/mytracks - আপনার ট্র্যাক লিস্ট\n\n"
            "<b>🔹 নোটিশ:</b>\n"
            "• বট ব্যবহার করতে চ্যানেল জয়েন করা আবশ্যক\n"
            "• প্রতি ৩০ মিনিট পর দাম চেক করা হয়\n"
            "• দাম কমলে আপনি নোটিফিকেশন পাবেন\n"
            "• ভালো অফার চ্যানেলেও পোস্ট করা হয়"
        )
        await query.edit_message_text(help_text, parse_mode=ParseMode.HTML)

# ========== অ্যাডমিন কমান্ড ==========
async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """অ্যাডমিন প্রোডাক্ট যোগ"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    try:
        url = context.args[0] if context.args else None
        if not url:
            await update.message.reply_text("ইউআরএল দিন: /add https://daraz.com...")
            return
        
        msg = await update.message.reply_text("⏳ যোগ করা হচ্ছে...")
        
        scraper = DarazScraper()
        product = await scraper.get_product_info(url)
        
        if not product:
            await msg.edit_text("❌ তথ্য পাওয়া যায়নি!")
            return
        
        products_collection.update_one(
            {"url": url},
            {
                "$set": {
                    "url": url,
                    "title": product.title,
                    "current_price": product.current_price,
                    "original_price": product.original_price,
                    "discount": product.discount_percentage,
                    "added_by": update.effective_user.id,
                    "added_at": datetime.utcnow(),
                    "last_checked": datetime.utcnow()
                }
            },
            upsert=True
        )
        
        await msg.edit_text(f"✅ যোগ করা হয়েছে!\n{product.title[:100]}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ সমস্যা: {e}")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """পরিসংখ্যান"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    total_products = products_collection.count_documents({})
    total_users = users_collection.count_documents({})
    total_tracks = user_tracks_collection.count_documents({})
    total_channel_posts = channel_posts_collection.count_documents({})
    active_tracks = user_tracks_collection.count_documents({"notified": False})
    
    # চ্যানেল জয়েন করা ইউজার
    channel_users = users_collection.count_documents({"channel_joined": True})
    
    msg = (
        f"<b>📊 বট পরিসংখ্যান</b>\n\n"
        f"📦 মোট প্রোডাক্ট: {total_products}\n"
        f"👥 মোট ইউজার: {total_users}\n"
        f"✅ চ্যানেল জয়েন: {channel_users}\n"
        f"🎯 মোট ট্র্যাক: {total_tracks}\n"
        f"⏳ সক্রিয় ট্র্যাক: {active_tracks}\n"
        f"📤 চ্যানেল পোস্ট: {total_channel_posts}\n"
        f"⏱ চেক ইন্টারভাল: {CHECK_INTERVAL} মিনিট"
    )
    
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ========== মেইন ==========
def main():
    """মেইন ফাংশন"""
    try:
        if not all([BOT_TOKEN, CHANNEL_ID, CHANNEL_LINK, MONGO_URI]):
            raise ValueError("সব environment variables সেট করুন!")
        
        app = Application.builder().token(BOT_TOKEN).build()
        
        job_queue = app.job_queue
        if job_queue:
            job_queue.run_repeating(
                price_monitor_job,
                interval=CHECK_INTERVAL * 60,
                first=10
            )
        
        # ইউজার কমান্ড
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("mytracks", my_tracks_command))
        
        # অ্যাডমিন কমান্ড
        app.add_handler(CommandHandler("add", admin_add))
        app.add_handler(CommandHandler("stats", admin_stats))
        
        # মেসেজ হ্যান্ডলার
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_track_request
        ))
        
        # কলব্যাক
        app.add_handler(CallbackQueryHandler(button_callback))
        
        logger.info("🚀 ম্যান্ডেটরি চ্যানেল বট চালু হচ্ছে...")
        print(f"✅ বট চালু হয়েছে! চ্যানেল: {CHANNEL_ID}")
        print("✅ ইউজারদের চ্যানেল জয়েন করা বাধ্যতামূলক!")
        
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"বট চালু করতে সমস্যা: {e}")
        raise

import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == "__main__":
    keep_alive()  # <--- এই লাইনটি যোগ করুন
    main()

