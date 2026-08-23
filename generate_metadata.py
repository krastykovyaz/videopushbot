"""
Генератор metadata для YouTube видео из PDF/TXT файлов
Использует Gemini API для создания описаний
"""

import os
import json
import logging
from pathlib import Path
import PyPDF2
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('metadata_generator.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

CONFIG = {
    # Папка для мониторинга (та же что для видео)
    'watch_folder': './videos_to_upload',
    # Папка для обработанных видео
    'processed_folder': './uploaded_videos',
    
    # Gemini API
    'gemini': {
        'api_key': os.getenv('GOOGLE_API_KEY', 'YOUR_GEMINI_API_KEY'),  # Получите на https://aistudio.google.com/apikey
        'model': "gemini-3-flash-preview",
    },
    
    # Плейлисты (название → ID)
    'playlists': {
                'AI Paper Review': 'PLgJoFV4tOm9OHGvu8XuFH_aEFna9oXjcZ',
                'Crypto Ideas': 'PLgJoFV4tOm9OwtIueKDgL-OYeDE0UaCU_',
                'GPMorgan report debates': 'PLgJoFV4tOm9Oye5OqNKri8DUAjuBoJGU0',
                'The Economist': 'PLgJoFV4tOm9MAw_dkw_DymL6my6Vp6nRQ',
                'The National Geo talks': 'PLgJoFV4tOm9NvS7K_SifypBtqjoQRDyKB',
            },
    
    # Поддерживаемые форматы
    'document_extensions': ['.pdf', '.txt', '.md'],
    
    # Thumbnail по умолчанию
    'default_thumbnail': 'unnamed.png',
}

# =============================================================================
# GEMINI API
# =============================================================================

class GeminiDescriptionGenerator:
    def __init__(self, api_key, model_name='gemini-pro'):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model_name)
        logging.info("✅ Gemini API инициализирован")
    
    def generate_youtube_description(self, title, content, max_length=5000):
        """Генерация описания для YouTube из содержимого документа"""
        
        # Обрезаем контент если слишком длинный (Gemini имеет лимиты)
        if len(content) > max_length:
            content = content[:max_length] + "..."
            logging.info(f"📄 Контент обрезан до {max_length} символов")
        
        prompt = f"""
You're an expert at creating YouTube video descriptions.

TASK:
Using this document, create a short, engaging description for a YouTube video.

DOCUMENT TITLE:
{title}

DOCUMENT CONTENTS:
{content}

DESCRIPTION REQUIREMENTS:
1. Length: 3-5 sentences (maximum 300 characters)
2. Style: Clear, engaging, for a general audience
3. Start with a compelling question or fact
4. Explain the main idea in simple language
5. Add 2-3 emojis for appeal
6. DO NOT use formal academic language
7. DO NOT copy text directly from the document

RESPONSE FORMAT:
Return ONLY the description text, without additional comments.

EXAMPLE OF A GOOD DESCRIPTION:
"Is AI about to change how we know what we know? 🧠✨ This paper explores how we can build AI systems that we can truly trust. Discover the key principles of epistemic reliability in artificial agents! 🤖📚"

Description:
"""
        
        try:
            logging.info("🤖 Генерация описания через Gemini...")
            response = self.model.generate_content(prompt)
            description = response.text.strip()
            

            logging.info(f"✅ Описание сгенерировано ({len(description)} символов)")
            return description
            
        except Exception as e:
            logging.error(f"❌ Ошибка Gemini API: {e}")
            return f"Обзор документа: {title}"

# =============================================================================
# ЧИТАЛКИ ДОКУМЕНТОВ
# =============================================================================

def read_pdf(file_path):
    """Чтение PDF файла"""
    try:
        with open(file_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            
            # Извлекаем заголовок из метаданных или первой страницы
            title = ""
            if pdf_reader.metadata and pdf_reader.metadata.title:
                title = pdf_reader.metadata.title
            
            # Читаем текст
            text = ""
            num_pages = min(len(pdf_reader.pages), 5)  # Первые 5 страниц
            
            for page_num in range(num_pages):
                page = pdf_reader.pages[page_num]
                text += page.extract_text()
            
            # Если заголовка нет в метаданных - берём первую строку
            if not title and text:
                first_lines = text.split('\n')[:3]
                title = ' '.join(first_lines).strip()[:100]
            
            logging.info(f"📄 PDF прочитан: {len(text)} символов, {num_pages} страниц")
            return title, text
            
    except Exception as e:
        logging.error(f"❌ Ошибка чтения PDF: {e}")
        return "", ""

def read_text(file_path):
    """Чтение TXT/MD файла"""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        
        # Заголовок - первая непустая строка
        lines = content.split('\n')
        title = ""
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                title = line
                break
        
        # Если markdown - убираем # из заголовка
        if title.startswith('#'):
            title = title.lstrip('#').strip()
        
        logging.info(f"📝 TXT прочитан: {len(content)} символов")
        return title, content
        
    except Exception as e:
        logging.error(f"❌ Ошибка чтения текста: {e}")
        return "", ""

def read_document(file_path):
    """Универсальная функция чтения документа"""
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.pdf':
        return read_pdf(file_path)
    elif ext in ['.txt', '.md']:
        return read_text(file_path)
    else:
        logging.error(f"❌ Неподдерживаемый формат: {ext}")
        return "", ""

# =============================================================================
# ИНТЕРАКТИВНЫЙ ВЫБОР ПЛЕЙЛИСТА
# =============================================================================

def select_playlist():
    """Интерактивный выбор плейлиста"""
    playlists = list(CONFIG['playlists'].keys())
    
    if not playlists:
        logging.warning("⚠️  Плейлисты не настроены в CONFIG")
        return None
    
    print()
    print("=" * 60)
    print("📋 Выберите плейлист:")
    print("=" * 60)
    
    for i, playlist_name in enumerate(playlists, 1):
        print(f"{i}. {playlist_name}")
    
    print("0. Без плейлиста")
    print("=" * 60)
    
    while True:
        try:
            choice = input("Введите номер (0-{}): ".format(len(playlists)))
            choice = int(choice)
            
            if choice == 0:
                return None
            elif 1 <= choice <= len(playlists):
                selected = playlists[choice - 1]
                logging.info(f"✅ Выбран плейлист: {selected}")
                return selected
            else:
                print("❌ Неверный номер, попробуйте снова")
        except ValueError:
            print("❌ Введите число")
        except KeyboardInterrupt:
            print("\n⏹️  Отменено")
            return None


def get_link():
    """put link to the document"""
    playlists = list(CONFIG['playlists'].keys())
    

    print()
    print("=" * 60)
    print("📋 Give me link to the source of the document:")
    print("=" * 60)
    
    try:
        choice = input("Link to the info of the doc: ")
        choice = choice
        
        logging.info(f"✅ Указана ссылка: {choice}")
        return choice
        # else:
        #     print("❌ Неверный номер, попробуйте снова")
    except ValueError:
        print("❌ Проблемы с вводом")
    except KeyboardInterrupt:
        print("\n⏹️  Отменено")
        return None

# =============================================================================
# ГЕНЕРАТОР METADATA
# =============================================================================

def process_document(file_path, gemini_generator):
    """Обработка документа и создание metadata.json"""
    
    file_name = os.path.basename(file_path)
    base_name = os.path.splitext(file_name)[0]
    output_dir = os.path.dirname(file_path)
    
    logging.info("=" * 60)
    logging.info(f"📄 Обработка: {file_name}")
    logging.info("=" * 60)
    
    # Читаем документ
    title, content = read_document(file_path)
    
    if not title or not content:
        logging.error(f"❌ Не удалось прочитать документ")
        return False
    
    # Очищаем заголовок
    title = title.replace('\n', ' ').strip()[:100]
    
    logging.info(f"📌 Заголовок: {title}")
    
    # Генерируем описание через Gemini
    description = gemini_generator.generate_youtube_description(title, content)
    link_to_document = get_link()
    marker = f"""\n\nDonats: https://www.patreon.com/c/luxak

paper - {link_to_document}
subscribe - https://t.me/arxivpaper
created with NotebookLM"""
    description += marker

    # Выбор плейлиста
    playlist = select_playlist()
    
    
    # Создаём metadata
    metadata = {
        "title": title,
        "description": description,
        "thumbnail": CONFIG['default_thumbnail'],
    }
    
    if playlist:
        metadata["playlist"] = playlist
    
    # Сохраняем JSON
    output_path = os.path.join(output_dir, f"{base_name}.json")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    logging.info("=" * 60)
    logging.info(f"✅ Metadata сохранён: {output_path}")
    logging.info("=" * 60)
    print()
    print("📄 РЕЗУЛЬТАТ:")
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    print()
    
    return True

def move_to_processed(file_path):
    """Перемещение в папку обработанных"""
    processed_path = os.path.join(
        CONFIG['processed_folder'],
        os.path.basename(file_path)
    )
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    os.rename(file_path, processed_path)
    logging.info(f"📦 Перемещено: {processed_path}")

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  YouTube Metadata Generator")
    print("  Генератор метаданных из PDF/TXT с помощью Gemini AI")
    print("=" * 70)
    print()
    
    # Проверка API ключа
    if CONFIG['gemini']['api_key'] == 'YOUR_GEMINI_API_KEY':
        print("❌ ОШИБКА: Вставьте Gemini API ключ в CONFIG!")
        print()
        print("Как получить:")
        print("1. Откройте https://aistudio.google.com/apikey")
        print("2. Нажмите 'Create API Key'")
        print("3. Скопируйте ключ")
        print("4. Вставьте в CONFIG['gemini']['api_key']")
        print()
        return
    
    # Инициализация Gemini
    try:
        gemini_generator = GeminiDescriptionGenerator(
            CONFIG['gemini']['api_key'],
            CONFIG['gemini']['model']
        )
    except Exception as e:
        logging.error(f"❌ Ошибка инициализации Gemini: {e}")
        return
    
    # Поиск документов в папке
    watch_folder = CONFIG['watch_folder']
    
    if not os.path.exists(watch_folder):
        os.makedirs(watch_folder)
        logging.info(f"📁 Создана папка: {watch_folder}")
    
    # Ищем документы
    documents = []
    for ext in CONFIG['document_extensions']:
        documents.extend(Path(watch_folder).glob(f"*{ext}"))
    
    if not documents:
        print(f"📂 Папка: {watch_folder}")
        print(f"📄 Поддерживаемые форматы: {', '.join(CONFIG['document_extensions'])}")
        print()
        print("⚠️  Документы не найдены!")
        print()
        print("Положите PDF или TXT файл в папку и запустите снова.")
        return
    
    print(f"📚 Найдено документов: {len(documents)}")
    print()
    
    # Обрабатываем каждый документ
    for doc_path in documents:
        # Пропускаем если уже есть JSON
        json_path = doc_path.with_suffix('.json')
        if json_path.exists():
            logging.info(f"⏭️  Пропускаем {doc_path.name} (metadata уже существует)")
            continue
        
        success = process_document(str(doc_path), gemini_generator)
        
        if success:
            move_to_processed(doc_path)
            print("✅ Готово! Теперь можно загружать видео с этим metadata.")
            print()
    
    print("=" * 70)
    print("✅ Все документы обработаны")
    print("=" * 70)

if __name__ == '__main__':
    main()