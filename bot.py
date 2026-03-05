import os
import requests
import pymongo
import asyncio
import re
import json
import gzip
from io import BytesIO
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime
from bson.objectid import ObjectId
from flask import Flask
from threading import Thread

# --- সেটিংস (Render এ Environment Variables হিসেবে সেট করুন) ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
CHECK_INTERVAL = 1800  # ৩০ মিনিট (সেকেন্ডে)

# MongoDB কানেকশন
client = pymongo.MongoClient(MONGO_URI)
db = client['daraz_tracker']
tracks_collection = db['tracks']
users_collection = db['users']
stats_collection = db['stats']


# ==============================================
# দারাজ পার্সার ক্লাস (আপডেটেড)
# ==============================================
class DarazParser:
    """শুধু দারাজের জন্য পার্সার - ব্রাউজার ইমুলেশন সহ"""
    
    @staticmethod
    def get_price(url):
        """দারাজ থেকে প্রাইস সংগ্রহ করুন"""
        
        # রিয়েল ব্রাউজারের মতো হেডার
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,bn;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        try:
            # টাইমআউট বাড়ানো হয়েছে
            response = requests.get(url, headers=headers, timeout=15)
            
            # gzip ডিকোড করা
            if response.headers.get('content-encoding') == 'gzip':
                response_content = gzip.decompress(response.content)
                soup = BeautifulSoup(response_content, 'html.parser')
            else:
                soup = BeautifulSoup(response.content, 'html.parser')
            
            # ========== প্রাইস সিলেক্টর ==========
            price_selectors = [
                # নতুন দারাজ ক্লাস
                ('span', {'class': 'pdp-price'}),
                ('span', {'class': 'pdp-price pdp-price--normal'}),
                ('span', {'class': 'pdp-price pdp-price--discount'}),
                ('div', {'class': 'pdp-product-price'}),
                ('span', {'class': 'not-module-price'}),
                
                # পুরনো ক্লাস
                ('span', {'class': 'sku-price'}),
                ('span', {'class': 'pdp-price_range'}),
                
                # আইডি বেসড
                ('div', {'id': 'module_product_price_1'}),
                ('span', {'id': 'module-currency'}),
            ]
            
            # প্রতিটি সিলেক্টর চেক করুন
            for tag, attrs in price_selectors:
                element = soup.find(tag, attrs)
                if element:
                    price_text = element.get_text()
                    price = DarazParser._clean_price(price_text)
                    if price and price > 0:
                        return price
            
            # ========== স্ক্রিপ্ট ট্যাগ থেকে JSON ডাটা ==========
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string and 'window.runParams' in script.string:
                    # window.runParams থেকে প্রাইস বের করা
                    match = re.search(r'"price":\s*"?(\d+\.?\d*)"?', script.string)
                    if match:
                        return float(match.group(1))
                    
                    # sku info থেকে প্রাইস
                    match = re.search(r'"skuId":.*?"price":\s*"?(\d+\.?\d*)"?', script.string)
                    if match:
                        return float(match.group(1))
            
            # ========== JSON-LD স্ক্রিপ্ট ==========
            json_scripts = soup.find_all('script', type='application/ld+json')
            for script in json_scripts:
                if script.string:
                    try:
                        data = json.loads(script.string)
                        if 'offers' in data and 'price' in data['offers']:
                            return float(data['offers']['price'])
                        elif 'price' in data:
                            return float(data['price'])
                    except:
                        pass
            
            # ========== মেটা ট্যাগ ==========
            meta_selectors = [
                {'property': 'product:price:amount'},
                {'name': 'price'},
                {'itemprop': 'price'},
                {'property': 'og:price:amount'},
            ]
            
            for attrs in meta_selectors:
                meta = soup.find('meta', attrs=attrs)
                if meta and meta.get('content'):
                    price = DarazParser._clean_price(meta['content'])
                    if price:
                        return price
            
            return None
            
        except requests.exceptions.Timeout:
            print(f"[{datetime.now()}] টাইমআউট: দারাজ রেসপন্স দিচ্ছে না")
            return None
        except Exception as e:
            print(f"[{datetime.now()}] দারাজ প্রাইস পার্সিং এ সমস্যা: {e}")
            return None
    
    @staticmethod
    def get_product_info(url):
        """পণ্যের তথ্য সংগ্রহ করুন (নাম, আইডি)"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # ========== পণ্যের নাম ==========
            title = None
            title_selectors = [
                ('h1', {'class': 'pdp-product-title'}),
                ('h1', {'class': 'pdp-mod-product-badge-title'}),
                ('span', {'class': 'pdp-mod-product-badge-title'}),
                ('h1', {'data-spm': '1001777'}),
            ]
            
            for tag, attrs in title_selectors:
                elem = soup.find(tag, attrs)
                if elem:
                    title = elem.get_text().strip()
                    break
            
            # টাইটেল ট্যাগ থেকে
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text().strip()
                    # "Price in Bangladesh" ইত্যাদি বাদ দেওয়া
                    title = re.sub(r'\s*[-|].*$', '', title)
            
            # ========== প্রোডাক্ট আইডি ==========
            product_id = None
            id_match = re.search(r'i(\d+)', url)
            if id_match:
                product_id = id_match.group(1)
            
            return {
                'title': title if title and len(title) > 5 else 'নাম জানা যায়নি',
                'product_id': product_id,
                'url': url
            }
            
        except Exception as e:
            print(f"[{datetime.now()}] পণ্যের তথ্য পাওয়া যায়নি: {e}")
            return {'title': 'নাম জানা যায়নি', 'product_id': None, 'url': url}
    
    @staticmethod
    def _clean_price(text):
        """প্রাইস টেক্সট ক্লিন করুন"""
        if not text:
            return None
        
        # বাংলা সংখ্যা ইংরেজিতে রূপান্তর
        bn_digits = {'০':'0', '১':'1', '২':'2', '৩':'3', '৪':'4', 
                     '৫':'5', '৬':'6', '৭':'7', '৮':'8', '৯':'9'}
        for bn, en in bn_digits.items():
            text = text.replace(bn, en)
        
        # ৳, টাকা, কমা, স্পেস রিমুভ
        text = re.sub(r'[^\d.]', ' ', text)
        
        # সব সংখ্যা বের করা
        numbers = re.findall(r'\d+\.?\d*', text)
        
        # ভ্যালিড প্রাইস ফিল্টার (১০ টাকা - ১০ লাখ টাকা)
        valid_prices = []
        for num in numbers:
            try:
                price = float(num)
                if 10 < price < 1000000:
                    valid_prices.append(price)
            except:
                pass
        
        if valid_prices:
            return max(valid_prices)  # সবচেয়ে বড় প্রাইস নেওয়া
        
        return None


# ==============================================
# দারাজ বট ক্লাস
# ==============================================
class DarazBot:
    def __init__(self):
        self.parser = DarazParser()
        self.bot_name = "দারাজ প্রাইস ট্র্যাকার 🇧🇩"
        
        # মেইন কীবোর্ড
        self.main_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 আমার ট্র্যাকিং", callback_data='list')],
            [InlineKeyboardButton("➕ নতুন ট্র্যাক", callback_data='add')],
            [InlineKeyboardButton("ℹ️ সাহায্য", callback_data='help')],
            [InlineKeyboardButton("📊 পরিসংখ্যান", callback_data='stats')]
        ])
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """স্টার্ট কমান্ড"""
        user = update.effective_user
        
        # ইউজার ইনফো সেভ
        users_collection.update_one(
            {'user_id': user.id},
            {'$set': {
                'username': user.username,
                'first_name': user.first_name,
                'last_active': datetime.now(),
                'total_tracks': tracks_collection.count_documents({'user_id': user.id})
            }},
            upsert=True
        )
        
        welcome_text = (
            f"👋 **স্বাগতম {user.first_name}!**\n\n"
            "আমি **দারাজ প্রাইস ট্র্যাকার বট** - আপনার পছন্দের পণ্যের দাম কমলে জানিয়ে দেব।\n\n"
            "**📝 কিভাবে ব্যবহার করবেন:**\n"
            "1️⃣ দারাজ থেকে পণ্যের লিঙ্ক কপি করুন\n"
            "2️⃣ বটে লিঙ্ক ও কাঙ্ক্ষিত দাম দিন\n"
            "3️⃣ দাম কমলে নোটিফিকেশন পাবেন\n\n"
            "**উদাহরণ:**\n"
            "`https://www.daraz.com.bd/products/example-i123456789.html 1500`\n\n"
            "**কমান্ড সমূহ:**\n"
            "/mytracks - আপনার ট্র্যাক করা পণ্য\n"
            "/help - সাহায্য\n"
            "/stats - পরিসংখ্যান\n\n"
            "নিচের বাটন ব্যবহার করুন 👇"
        )
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=self.main_keyboard
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """বাটন ক্লিক হ্যান্ডলার"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'list':
            await self.list_tracks(update, context)
        elif query.data == 'add':
            await query.edit_message_text(
                "🔍 **নতুন পণ্য ট্র্যাক করুন**\n\n"
                "একটি মেসেজে পাঠান:\n"
                "`<দারাজ লিঙ্ক> <কাঙ্ক্ষিত দাম>`\n\n"
                "**উদাহরণ:**\n"
                "`https://www.daraz.com.bd/products/smartphone-i123456789.html 1500`\n\n"
                "**নিয়ম:**\n"
                "• শুধু দারাজের লিঙ্ক দিন\n"
                "• দাম সংখ্যায় দিন (যেমন: 1500)\n"
                "• প্রতি ইউজার সর্বোচ্চ ১০টি পণ্য ট্র্যাক করতে পারবেন",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 ব্যাক", callback_data='back_to_main')
                ]])
            )
        elif query.data == 'help':
            await self.help(update, context)
        elif query.data == 'stats':
            await self.stats(update, context)
        elif query.data == 'back_to_main':
            await query.edit_message_text(
                "🏠 **মেনুতে ফিরে আসুন**\n\nনিচের বাটন ব্যবহার করুন:",
                reply_markup=self.main_keyboard
            )
        elif query.data.startswith('remove_'):
            track_id = query.data.replace('remove_', '')
            result = tracks_collection.delete_one({'_id': ObjectId(track_id)})
            
            if result.deleted_count > 0:
                await query.edit_message_text(
                    "✅ **ট্র্যাকটি মুছে ফেলা হয়েছে!**",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 আমার ট্র্যাকিং", callback_data='list')
                    ]])
                )
            else:
                await query.edit_message_text(
                    "❌ **ট্র্যাকটি পাওয়া যায়নি!**",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 আমার ট্র্যাকিং", callback_data='list')
                    ]])
                )
    
    async def add_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """নতুন ট্র্যাক যোগ করুন"""
        try:
            text = update.message.text.strip()
            parts = text.split()
            
            if len(parts) < 2:
                await update.message.reply_text(
                    "❌ **ভুল ফরম্যাট!**\n\n"
                    "সঠিক ফরম্যাট: `<লিঙ্ক> <টার্গেট প্রাইস>`\n"
                    "উদাহরণ: `https://www.daraz.com.bd/products/1234 1500`",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 মেনুতে ফিরুন", callback_data='back_to_main')
                    ]])
                )
                return
            
            url = parts[0]
            target_price = float(parts[1])
            user_id = update.effective_user.id
            
            # দারাজ লিঙ্ক ভেরিফিকেশন
            if 'daraz' not in url.lower():
                await update.message.reply_text(
                    "❌ **শুধু দারাজের লিঙ্ক দিন!**\n"
                    "উদাহরণ: daraz.com.bd/products/..."
                )
                return
            
            # ইউজারের ট্র্যাক লিমিট চেক (সর্বোচ্চ ১০টি)
            user_tracks = tracks_collection.count_documents({'user_id': user_id})
            if user_tracks >= 10:
                await update.message.reply_text(
                    "⚠️ **আপনি সর্বোচ্চ ১০টি পণ্য ট্র্যাক করতে পারবেন!**\n"
                    "পুরনো ট্র্যাক মুছে নতুন করুন।",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 আমার ট্র্যাকিং", callback_data='list')
                    ]])
                )
                return
            
            # প্রাইস চেক করা হচ্ছে
            status_msg = await update.message.reply_text("⏳ দারাজ থেকে তথ্য সংগ্রহ করা হচ্ছে...")
            
            # পণ্যের তথ্য ও প্রাইস সংগ্রহ
            product_info = self.parser.get_product_info(url)
            current_price = self.parser.get_price(url)
            
            # দ্বিতীয়বার চেষ্টা যদি প্রথমবার না পায়
            if not current_price:
                await asyncio.sleep(2)
                current_price = self.parser.get_price(url)
            
            # ডাটাবেজে সেভ
            track_data = {
                'user_id': user_id,
                'url': url,
                'title': product_info['title'],
                'product_id': product_info['product_id'],
                'target_price': target_price,
                'current_price': current_price,
                'created_at': datetime.now(),
                'last_checked': datetime.now(),
                'notified': False,
                'status': 'active'
            }
            tracks_collection.insert_one(track_data)
            
            # স্ট্যাটস আপডেট
            stats_collection.update_one(
                {'date': datetime.now().strftime('%Y-%m-%d')},
                {'$inc': {'total_tracks': 1}},
                upsert=True
            )
            
            # রেসপন্স তৈরি
            if current_price:
                response = f"✅ **ট্র্যাকিং শুরু হয়েছে!**\n\n"
                response += f"📦 **পণ্য:** {product_info['title']}\n"
                response += f"💰 **বর্তমান দাম:** ৳{current_price}\n"
                response += f"🎯 **টার্গেট মূল্য:** ৳{target_price}\n"
                
                if current_price <= target_price:
                    response += f"⚠️ **বর্তমান দাম ই আপনার টার্গেটের চেয়ে কম!**\n"
            else:
                response = f"⚠️ **প্রোডাক্ট তথ্য পাওয়া যায়নি!**\n\n"
                response += f"📦 **পণ্য:** {product_info['title']}\n"
                response += f"🎯 **টার্গেট:** ৳{target_price}\n\n"
                response += f"আমি পরে আবার চেক করব। দাম কমলে জানাব!"
            
            await status_msg.edit_text(response)
            
        except ValueError:
            await update.message.reply_text("❌ **দাম সঠিক সংখ্যা দিন!**\nউদাহরণ: 1500")
        except Exception as e:
            await update.message.reply_text(f"❌ **এরর হয়েছে:** {str(e)}")
    
    async def list_tracks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ট্র্যাক লিস্ট দেখান"""
        user_id = update.effective_user.id
        
        tracks = list(tracks_collection.find({'user_id': user_id}).sort('created_at', -1))
        
        if not tracks:
            msg = "📭 **আপনার কোনো ট্র্যাক করা পণ্য নেই!**\n\nনতুন পণ্য ট্র্যাক করতে লিঙ্ক ও দাম পাঠান।"
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    msg,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕ নতুন ট্র্যাক", callback_data='add')
                    ]])
                )
            else:
                await update.message.reply_text(msg)
            return
        
        msg = f"📋 **আপনার ট্র্যাক করা পণ্য ({len(tracks)}টি)**\n\n"
        keyboard = []
        
        for i, track in enumerate(tracks, 1):
            # স্ট্যাটাস ইমোজি
            if track.get('notified'):
                status = "✅ নোটিফাই করা হয়েছে"
            elif track.get('current_price') and track['current_price'] <= track['target_price']:
                status = "💰 টার্গেট অর্জিত"
            else:
                status = "⏳ ট্র্যাকিং চলছে"
            
            msg += f"**{i}. {track['title'][:50]}**\n"
            msg += f"   🎯 টার্গেট: ৳{track['target_price']}\n"
            msg += f"   💰 বর্তমান: ৳{track.get('current_price', 'অজানা')}\n"
            msg += f"   📊 স্ট্যাটাস: {status}\n"
            msg += f"   [🔗 লিঙ্ক]({track['url']})\n\n"
            
            # রিমুভ বাটন
            keyboard.append([InlineKeyboardButton(
                f"❌ মুছুন {i}", 
                callback_data=f"remove_{track['_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 মেনু", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                msg,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(
                msg,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
    
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """হেল্প মেসেজ"""
        help_text = (
            "📚 **দারাজ প্রাইস ট্র্যাকার বট - সাহায্য**\n\n"
            "**কমান্ড সমূহ:**\n"
            "/start - বট শুরু করুন\n"
            "/mytracks - আপনার ট্র্যাক করা পণ্য দেখুন\n"
            "/help - এই মেসেজ\n"
            "/stats - বট পরিসংখ্যান\n\n"
            
            "**কিভাবে ব্যবহার করবেন:**\n"
            "1️⃣ দারাজ অ্যাপ বা ওয়েবসাইট থেকে পণ্যের লিঙ্ক কপি করুন\n"
            "2️⃣ বটে লিঙ্ক এবং আপনার কাঙ্ক্ষিত দাম দিন\n"
            "3️⃣ বট স্বয়ংক্রিয়ভাবে প্রতি ৩০ মিনিট পর প্রাইস চেক করবে\n"
            "4️⃣ দাম কমলে আপনি নোটিফিকেশন পাবেন\n\n"
            
            "**উদাহরণ:**\n"
            "`https://www.daraz.com.bd/products/smartphone-i123456789.html 1500`\n\n"
            
            "**নিয়ম:**\n"
            "• শুধু দারাজের পণ্য ট্র্যাক করা যায়\n"
            "• সর্বোচ্চ ১০টি পণ্য ট্র্যাক করতে পারবেন\n"
            "• দাম বাংলা বা ইংরেজি সংখ্যা দুই-ই দেওয়া যাবে\n\n"
            
            "🔗 **আপডেট ও সাপোর্ট:** @YourChannel"
        )
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                help_text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 ব্যাক", callback_data='back_to_main')
                ]])
            )
        else:
            await update.message.reply_text(help_text)
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """বট পরিসংখ্যান"""
        total_users = users_collection.count_documents({})
        total_tracks = tracks_collection.count_documents({})
        active_tracks = tracks_collection.count_documents({'status': 'active'})
        notified_tracks = tracks_collection.count_documents({'notified': True})
        
        # আজকের স্ট্যাটস
        today = datetime.now().strftime('%Y-%m-%d')
        today_stats = stats_collection.find_one({'date': today})
        today_tracks = today_stats['total_tracks'] if today_stats else 0
        
        stats_text = (
            f"📊 **দারাজ বট পরিসংখ্যান**\n\n"
            f"👥 **মোট ইউজার:** {total_users}\n"
            f"📦 **মোট ট্র্যাক:** {total_tracks}\n"
            f"✅ **এক্টিভ ট্র্যাক:** {active_tracks}\n"
            f"🎉 **নোটিফিকেশন:** {notified_tracks}\n"
            f"📅 **আজকের ট্র্যাক:** {today_tracks}\n\n"
            f"**বট ভার্সন:** 2.0 (শুধু দারাজ)\n"
            f"**চেক ইন্টারভাল:** ৩০ মিনিট"
        )
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 ব্যাক", callback_data='back_to_main')
                ]])
            )
        else:
            await update.message.reply_text(stats_text)
    
    async def price_checker_job(self, context: ContextTypes.DEFAULT_TYPE):
        """JobQueue দিয়ে প্রাইস চেক করা"""
        parser = DarazParser()
        bot = context.bot
        
        print(f"\n[{datetime.now()}] 🔍 JobQueue: দারাজ প্রাইস চেক করা হচ্ছে...")
        
        try:
            # সব এক্টিভ ট্র্যাক
            active_tracks = tracks_collection.find({'notified': False})
            checked = 0
            notified = 0
            
            for track in active_tracks:
                try:
                    current_price = parser.get_price(track['url'])
                    
                    if current_price:
                        # আপডেট প্রাইস
                        tracks_collection.update_one(
                            {'_id': track['_id']},
                            {'$set': {
                                'current_price': current_price,
                                'last_checked': datetime.now()
                            }}
                        )
                        
                        # চেক টার্গেট প্রাইস
                        if current_price <= track['target_price']:
                            # নোটিফিকেশন পাঠান
                            message = (
                                f"🎉 **দাম কমেছে!**\n\n"
                                f"📦 **পণ্য:** {track['title']}\n"
                                f"💰 **বর্তমান দাম:** ৳{current_price}\n"
                                f"🎯 **আপনার টার্গেট:** ৳{track['target_price']}\n"
                                f"📉 **কমেছে:** ৳{track['target_price'] - current_price}\n\n"
                                f"🔗 [পণ্যটি দেখুন]({track['url']})"
                            )
                            
                            try:
                                await bot.send_message(
                                    chat_id=track['user_id'],
                                    text=message,
                                    parse_mode='Markdown'
                                )
                                
                                # নোটিফাইড মার্ক করুন
                                tracks_collection.update_one(
                                    {'_id': track['_id']},
                                    {'$set': {'notified': True}}
                                )
                                
                                notified += 1
                                
                                # স্ট্যাটস আপডেট
                                stats_collection.update_one(
                                    {'date': datetime.now().strftime('%Y-%m-%d')},
                                    {'$inc': {'notifications': 1}},
                                    upsert=True
                                )
                                
                            except Exception as e:
                                print(f"নোটিফিকেশন পাঠাতে সমস্যা: {e}")
                    
                    checked += 1
                    
                except Exception as e:
                    print(f"ট্র্যাক চেক করতে সমস্যা: {e}")
                    continue
            
            print(f"✅ JobQueue চেক সম্পন্ন: {checked} টি চেক করা, {notified} টি নোটিফিকেশন পাঠানো হয়েছে")
            
        except Exception as e:
            print(f"❌ JobQueue প্রাইস চেকারে বড় সমস্যা: {e}")


# ==============================================
# ফ্লাস্ক সার্ভার (Render-এর জন্য)
# ==============================================
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 দারাজ প্রাইস ট্র্যাকার বট চলছে! 🇧🇩"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()


# ==============================================
# প্রাইস চেকার ফাংশন (JobQueue-র জন্য)
# ==============================================
async def price_checker_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue দিয়ে প্রাইস চেক করা"""
    parser = DarazParser()
    bot = context.bot
    
    print(f"\n[{datetime.now()}] 🔍 প্রাইস চেক করা হচ্ছে...")
    
    try:
        active_tracks = tracks_collection.find({'notified': False})
        checked = 0
        notified = 0
        
        for track in active_tracks:
            try:
                current_price = parser.get_price(track['url'])
                
                if current_price:
                    tracks_collection.update_one(
                        {'_id': track['_id']},
                        {'$set': {'current_price': current_price}}
                    )
                    
                    if current_price <= track['target_price']:
                        message = (
                            f"🎉 **দাম কমেছে!**\n\n"
                            f"📦 **পণ্য:** {track['title']}\n"
                            f"💰 **বর্তমান:** ৳{current_price}\n"
                            f"🎯 **টার্গেট:** ৳{track['target_price']}"
                        )
                        
                        await bot.send_message(
                            chat_id=track['user_id'],
                            text=message
                        )
                        
                        tracks_collection.update_one(
                            {'_id': track['_id']},
                            {'$set': {'notified': True}}
                        )
                        
                        notified += 1
                
                checked += 1
                
            except Exception as e:
                print(f"ট্র্যাক চেক করতে সমস্যা: {e}")
                continue
        
        print(f"✅ চেক সম্পন্ন: {checked} টি চেক করা, {notified} টি নোটিফিকেশন")
        
    except Exception as e:
        print(f"❌ প্রাইস চেকারে সমস্যা: {e}")


# ==============================================
# মেইন ফাংশন
# ==============================================
def main():
    """বট চালু করুন"""
    # ফ্লাস্ক সার্ভার চালু করুন
    keep_alive()
    print("🌐 ফ্লাস্ক সার্ভার চালু হয়েছে")
    
    # বট ইনিশিয়ালাইজ
    daraz_bot = DarazBot()
    
    # Application বিল্ড করুন
    application = ApplicationBuilder().token(TOKEN).build()
    
    # হ্যান্ডলার যোগ করুন
    application.add_handler(CommandHandler("start", daraz_bot.start))
    application.add_handler(CommandHandler("mytracks", daraz_bot.list_tracks))
    application.add_handler(CommandHandler("help", daraz_bot.help))
    application.add_handler(CommandHandler("stats", daraz_bot.stats))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, daraz_bot.add_track))
    application.add_handler(CallbackQueryHandler(daraz_bot.button_handler))
    
    # JobQueue সেটআপ - প্রতি ৩০ মিনিট পর প্রাইস চেক করবে
    job_queue = application.job_queue
    job_queue.run_repeating(price_checker_job, interval=CHECK_INTERVAL, first=10)
    
    print("="*60)
    print("🤖 দারাজ প্রাইস ট্র্যাকার বট চালু হয়েছে")
    print(f"📊 চেক ইন্টারভাল: {CHECK_INTERVAL//60} মিনিট")
    print(f"👥 মোট ইউজার: {users_collection.count_documents({})}")
    print(f"📦 মোট ট্র্যাক: {tracks_collection.count_documents({})}")
    print("="*60)
    
    # বট চালান
    application.run_polling()

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

