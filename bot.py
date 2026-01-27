import os
import random
import asyncio
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from telegram.request import HTTPXRequest
import glob
from dotenv import load_dotenv

load_dotenv()

def parse_tests(filename):
    questions = []
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    blocks = content.strip().split('++++')
    for block in blocks:
        if not block.strip():
            continue
        lines = block.strip().split('\n')
        question = lines[0].strip()
        options = []
        correct_id = 0
        
        for line in lines[1:]:
            if line.startswith('===='):
                opt = line[4:].strip()
                if opt.startswith('#'):
                    correct_id = len(options)
                    opt = opt[1:].strip()
                options.append(opt)
        
        if question and len(options) >= 2:
            questions.append({
                'question': question,
                'options': options,
                'correct_id': correct_id
            })
    return questions

# State constants for Admin Conversation
ADD_SUBJECT_NAME, ADD_SUBJECT_FILE = range(2)

SUBJECTS = {} # {subject_name: [questions]}
DATA_DIR = os.path.join('data', 'subjects')

def load_all_subjects():
    global SUBJECTS
    SUBJECTS = {}
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        
    for file_path in glob.glob(os.path.join(DATA_DIR, '*.txt')):
        subject_name = os.path.splitext(os.path.basename(file_path))[0]
        questions = parse_tests(file_path)
        if questions:
            SUBJECTS[subject_name] = questions
    
    print(f"✅ Yuklangan fanlar: {list(SUBJECTS.keys())}")
    
    # "Barchasidan" (Mixed) virtual subject qo'shish
    if len(SUBJECTS) > 1:
        all_questions = []
        for q_list in SUBJECTS.values():
            all_questions.extend(q_list)
        random.shuffle(all_questions)
        SUBJECTS["🎲 Barchasidan"] = all_questions

load_all_subjects()
SECTION_SIZE = 50
QUESTION_TIME = 10
ADMIN_ID = os.getenv('ADMIN_ID')
if ADMIN_ID:
    try:
        ADMIN_ID = int(ADMIN_ID)
    except ValueError:
        ADMIN_ID = None

chat_data = {}
user_names = {}
active_polls = {}
running_tasks = {}  # {chat_id: asyncio.Task}

def get_subject_keyboard():
    keyboard = []
    subjects = sorted(list(SUBJECTS.keys()))
    row = []
    for sub in subjects:
        row.append(InlineKeyboardButton(sub, callback_data=f"sub_{sub}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

def get_sections(subject_name):
    questions = SUBJECTS.get(subject_name, [])
    total = len(questions)
    sections = []
    for i in range(0, total, SECTION_SIZE):
        end = min(i + SECTION_SIZE, total)
        sections.append((i, end))
    return sections

def get_section_keyboard(subject_name):
    sections = get_sections(subject_name)
    keyboard = []
    row = []
    for i, (start, end) in enumerate(sections):
        btn = InlineKeyboardButton(f"{start+1}-{end}", callback_data=f"sec_{i}")
        row.append(btn)
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🎲 Tasodifiy", callback_data="sec_random")])
    keyboard.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="back_to_subjects")])
    return InlineKeyboardMarkup(keyboard)

async def quiz_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Asosiy quiz loop - har 20 sekundda yangi savol"""
    while chat_id in chat_data:
        data = chat_data[chat_id]
        idx = data['current']
        end_idx = data['section_end']
        start_idx = data['section_start']
        
        # Bo'lim tugadi
        if idx >= end_idx:
            scores = data['scores']
            total = end_idx - start_idx
            
            text = f"✅ Bo'lim tugadi! ({start_idx+1}-{end_idx})\n\n🏆 Natijalar:\n\n"
            
            if scores:
                sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
                medals = ["🥇", "🥈", "🥉"]
                
                for i, (uid, score) in enumerate(sorted_scores[:10]):
                    name = user_names.get(uid, f"User {uid}")
                    medal = medals[i] if i < 3 else f"{i+1}."
                    percent = (score / total) * 100
                    if percent >= 86:
                        grade = "A'lo"
                    elif percent >= 71:
                        grade = "Yaxshi"
                    elif percent >= 56:
                        grade = "Qoniqarli"
                    else:
                        grade = "Yiqildi"
                    text += f"{medal} {name}: {score}/{total} ({percent:.0f}%) - {grade}\n"
            else:
                text += "Hech kim javob bermadi.\n"
            
            text += "\n/start - Yangi bo'lim"
            
            await context.bot.send_message(chat_id=chat_id, text=text)
            
            if chat_id in chat_data:
                del chat_data[chat_id]
            if chat_id in running_tasks:
                del running_tasks[chat_id]
            break
        
        subject_name = data.get('subject')
        questions = SUBJECTS.get(subject_name, [])
        if not questions:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Xatolik: Fan topilmadi.")
            break
            
        q = questions[idx]
        question_num = idx - start_idx + 1
        total_in_section = end_idx - start_idx
        
        # Variantlar o'rnini almashtirish
        options = list(q['options'])
        correct_text = options[q['correct_id']]
        random.shuffle(options)
        new_correct_id = options.index(correct_text)
        
        # Poll yuborish
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"[{question_num}/{total_in_section}] {q['question']}",
            options=options,
            type=Poll.QUIZ,
            correct_option_id=new_correct_id,
            is_anonymous=False,
            open_period=QUESTION_TIME
        )
        
        active_polls[chat_id] = {
            'poll_id': msg.poll.id,
            'correct_id': new_correct_id,
            'answered_users': set()
        }
        
        chat_data[chat_id]['current'] = idx + 1
        
        # Vaqt kutish
        await asyncio.sleep(QUESTION_TIME + 2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_names[user.id] = user.first_name
    
    if chat_id in running_tasks:
        await update.message.reply_text("⚠️ Test davom etmoqda!\n/stop - To'xtatish")
        return
    
    # Eskidan qolgan tanlovlarni tozalash
    if chat_id in chat_data:
        del chat_data[chat_id]
    
    if not SUBJECTS:
        await update.message.reply_text("😔 Hozircha testlar yo'q. Adminni kuting.")
        return

    await update.message.reply_text(
        f"🎓 Shukurjon Boqiyev Quiz Bot\n\n"
        f"📚 {len(SUBJECTS)} ta fan mavjud\n"
        f"⏱ Har bir savol {QUESTION_TIME} sek\n\n"
        f"Fanni tanlang:",
        reply_markup=get_subject_keyboard()
    )

async def subject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    subject_name = query.data[4:] # "sub_" dan keyingi qism
    
    if subject_name not in SUBJECTS:
        await query.edit_message_text("⚠️ Fan topilmadi.", reply_markup=get_subject_keyboard())
        return
        
    chat_id = query.message.chat_id
    if chat_id not in chat_data:
        chat_data[chat_id] = {}
    chat_data[chat_id]['subject'] = subject_name
    
    await query.edit_message_text(
        f"📚 Fan: {subject_name}\n"
        f"📝 Jammi savollar: {len(SUBJECTS[subject_name])} ta\n\n"
        f"Bo'limni tanlang:",
        reply_markup=get_section_keyboard(subject_name)
    )

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Fanni tanlang:", reply_markup=get_subject_keyboard())

async def section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    data = query.data
    
    if chat_id in running_tasks:
        await query.answer("Test davom etmoqda!", show_alert=True)
        return
    
    subject_name = chat_data.get(chat_id, {}).get('subject')
    if not subject_name:
        await query.answer("Iltimos, fanni tanlang!", show_alert=True)
        return
        
    sections = get_sections(subject_name)
    
    if data == "sec_random":
        section_idx = random.randint(0, len(sections) - 1)
    else:
        try:
            section_idx = int(data.split("_")[1])
        except (IndexError, ValueError):
            return
    
    start_idx, end_idx = sections[section_idx]
    
    chat_data[chat_id].update({
        'section_start': start_idx,
        'section_end': end_idx,
        'current': start_idx,
        'scores': {}
    })
    
    await query.edit_message_text(
        f"✅ Bo'lim: {start_idx+1}-{end_idx}\n"
        f"📝 {end_idx - start_idx} ta savol\n"
        f"⏱ Har biri {QUESTION_TIME} sek\n\n"
        f"🚀 Boshlanmoqda..."
    )
    
    # Quiz loop boshlash
    task = asyncio.create_task(quiz_loop(context, chat_id))
    running_tasks[chat_id] = task

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if chat_id not in chat_data:
        await update.message.reply_text("❌ Faol test yo'q.")
        return
    
    data = chat_data[chat_id]
    scores = data.get('scores', {})
    current = data.get('current', data.get('section_start', 0))
    start_idx = data.get('section_start', 0)
    answered = current - start_idx
    
    # Task bekor qilish
    if chat_id in running_tasks:
        running_tasks[chat_id].cancel()
        del running_tasks[chat_id]
    
    text = f"⏹ Test to'xtatildi!\n📊 {answered} ta savol\n\n"
    
    if scores:
        text += "🏆 Natijalar:\n"
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        
        for i, (uid, score) in enumerate(sorted_scores[:10]):
            name = user_names.get(uid, f"User {uid}")
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} {name}: {score}\n"
    
    text += "\n/start - Yangi test"
    
    if chat_id in active_polls:
        del active_polls[chat_id]
    if chat_id in chat_data:
        del chat_data[chat_id]
    
    await update.message.reply_text(text)

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.poll_answer.user.id
    poll_id = update.poll_answer.poll_id
    user_names[user_id] = update.poll_answer.user.first_name
    
    if not update.poll_answer.option_ids:
        return
    
    selected = update.poll_answer.option_ids[0]
    
    for chat_id, poll_info in list(active_polls.items()):
        if poll_info['poll_id'] == poll_id:
            if user_id not in poll_info['answered_users']:
                poll_info['answered_users'].add(user_id)
                
                if chat_id in chat_data:
                    if user_id not in chat_data[chat_id]['scores']:
                        chat_data[chat_id]['scores'][user_id] = 0
                    
                    if selected == poll_info['correct_id']:
                        chat_data[chat_id]['scores'][user_id] += 1
            break

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if chat_id not in chat_data:
        await update.message.reply_text("❌ Test yo'q. /start")
        return
    
    scores = chat_data[chat_id]['scores']
    current = chat_data[chat_id]['current'] - chat_data[chat_id]['section_start']
    
    if not scores:
        await update.message.reply_text("Hali javob yo'q.")
        return
    
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    text = f"📊 Natijalar ({current} savol):\n\n"
    medals = ["🥇", "🥈", "🥉"]
    
    for i, (uid, score) in enumerate(sorted_scores[:10]):
        name = user_names.get(uid, f"User {uid}")
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {name}: {score}\n"
    
    await update.message.reply_text(text)

# --- ADMIN HANDLERS ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
        
    keyboard = [
        [InlineKeyboardButton("➕ Fan qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton("🗑 Fan o'chirish", callback_data="adm_del")],
        [InlineKeyboardButton("📋 Fanlar ro'yxati", callback_data="adm_list")],
        [InlineKeyboardButton("🔄 Yangilash", callback_data="adm_reload")]
    ]
    await update.message.reply_text("🛠 Admin paneli:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await query.answer("Siz admin emassiz!", show_alert=True)
        return
        
    data = query.data
    await query.answer()
    
    if data == "adm_add":
        await query.message.reply_text("Yangi fan nomini kiriting (yoki /cancel):")
        return ADD_SUBJECT_NAME
    
    elif data == "adm_del":
        if not SUBJECTS:
            await query.edit_message_text("Fanni o'chirish uchun avval fan qo'shing.")
            return
        keyboard = []
        for sub in sorted(SUBJECTS.keys()):
            keyboard.append([InlineKeyboardButton(f"❌ {sub}", callback_data=f"confirm_del_{sub}")])
        keyboard.append([InlineKeyboardButton("⬅️ Admin Menu", callback_data="adm_back")])
        await query.edit_message_text("O'chirmoqchi bo'lgan fanni tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "adm_list":
        if not SUBJECTS:
            text = "😔 Hozircha fanlar yo'q."
        else:
            text = "📋 Mavjud fanlar:\n\n"
            for i, sub in enumerate(sorted(SUBJECTS.keys()), 1):
                count = len(SUBJECTS[sub])
                text += f"{i}. {sub} ({count} ta savol)\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Admin Menu", callback_data="adm_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "adm_reload":
        load_all_subjects()
        await query.edit_message_text(f"✅ Ma'lumotlar yangilandi. {len(SUBJECTS)} ta fan yuklandi.", 
                                   reply_markup=query.message.reply_markup)
    
    elif data == "adm_back":
        keyboard = [
            [InlineKeyboardButton("➕ Fan qo'shish", callback_data="adm_add")],
            [InlineKeyboardButton("🗑 Fan o'chirish", callback_data="adm_del")],
            [InlineKeyboardButton("📋 Fanlar ro'yxati", callback_data="adm_list")],
            [InlineKeyboardButton("🔄 Yangilash", callback_data="adm_reload")]
        ]
        await query.edit_message_text("🛠 Admin paneli:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("confirm_del_"):
        sub_to_del = data[12:]
        file_path = os.path.join(DATA_DIR, f"{sub_to_del}.txt")
        if os.path.exists(file_path):
            os.remove(file_path)
            load_all_subjects()
            await query.edit_message_text(f"✅ {sub_to_del} o'chirildi.")
        else:
            await query.edit_message_text("❌ Fayl topilmadi.")

async def add_subject_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if name.startswith('/'): return
    context.user_data['new_sub_name'] = name
    await update.message.reply_text(f"Endi '{name}' uchun .txt faylni yuboring:")
    return ADD_SUBJECT_FILE

async def add_subject_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get('new_sub_name')
    if not update.message.document:
        await update.message.reply_text("Iltimos, .txt fayl yuboring!")
        return ADD_SUBJECT_FILE
        
    doc = update.message.document
    if not doc.file_name.endswith('.txt'):
        await update.message.reply_text("Faqat .txt fayl!")
        return ADD_SUBJECT_FILE
        
    file_path = os.path.join(DATA_DIR, f"{name}.txt")
    bot_file = await context.bot.get_file(doc.file_id)
    await bot_file.download_to_drive(file_path)
    
    load_all_subjects()
    await update.message.reply_text(f"✅ '{name}' muvaffaqiyatli qo'shildi!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.")
    return ConversationHandler.END

def main():
    token = os.getenv('BOT_TOKEN')
    proxy_url = os.getenv('PROXY_URL')
    
    if not token:
        print("❌ BOT_TOKEN topilmadi!")
        return
    
    request_kwargs = {
        "connect_timeout": 30.0,
        "read_timeout": 30.0,
        "write_timeout": 30.0,
        "pool_timeout": 30.0,
        "connection_pool_size": 10
    }
    
    if proxy_url and proxy_url.strip() and not proxy_url.startswith('#'):
        url = proxy_url.strip()
        if '://' in url:
            print(f"🌐 Proxy ishlatilmoqda: {url}")
            request_kwargs["proxy"] = url
        
    request = HTTPXRequest(**request_kwargs)
    app = Application.builder().token(token).request(request).build()
    
    # Admin Conversation
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^adm_add$")],
        states={
            ADD_SUBJECT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subject_name)],
            ADD_SUBJECT_FILE: [MessageHandler(filters.Document.ALL, add_subject_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("admin", admin_menu))
    
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^confirm_del_"))
    app.add_handler(CallbackQueryHandler(subject_callback, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(back_callback, pattern="back_to_subjects"))
    app.add_handler(CallbackQueryHandler(section_callback, pattern="^sec_"))
    app.add_handler(PollAnswerHandler(poll_answer))
    
    print(f"✅ Bot: {len(SUBJECTS)} ta fan yuklandi")
    print("🚀 Bot ishga tushmoqda...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
