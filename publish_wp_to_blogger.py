import requests
import os
import re
import json
import datetime
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
import markdown
import google.generativeai as genai

# --- Konfigurasi ---
# INI ADALAH URL API WORDPRESS SELF-HOSTED KAMU YANG KITA FOKUSKAN
API_BASE_URL = "https://ngesex.org/wp-json/wp/v2/posts"
STATE_FILE = 'published_posts.json' # File untuk melacak postingan yang sudah diterbitkan

# --- Konfigurasi Gemini API ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable not set. Please set it in your GitHub Secrets or local environment.")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash") # Menentukan model secara eksplisit

# --- Konfigurasi Blogger ---
BLOGGER_BLOG_ID = os.getenv("BLOGGER_BLOG_ID")
# GOOGLE_APPLICATION_CREDENTIALS_JSON_STRING akan dibaca dari environment variable di workflow GitHub Actions.

# --- Penggantian Kata Khusus ---
REPLACEMENT_MAP = {
    "memek": "serambi lempit",
    "kontol": "rudal",
    "ngentot": "menggenjot",
    "vagina": "serambi lempit",
    "penis": "rudal",
    "seks": "bercinta",
    "mani": "kenikmatan",
    "sex": "bercinta"
}

# === Utilitas ===

def extract_first_image_url(html_content):
    """
    Mencari URL gambar pertama di dalam konten HTML.
    """
    match = re.search(r'<img[^>]+src="([^"]+)"', html_content, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def strip_html_and_divs(html):
    """
    Menghapus sebagian besar tag HTML, kecuali yang esensial,
    dan mengganti </p> dengan dua newline untuk pemisahan paragraf.
    """
    html_with_newlines = re.sub(r'</p>', r'\n\n', html, flags=re.IGNORECASE)
    html_no_images = re.sub(r'<img[^>]*>', '', html_with_newlines)
    html_no_divs = re.sub(r'</?div[^>]*>', '', html_no_images, flags=re.IGNORECASE)
    clean_text = re.sub('<[^<]+?>', '', html_no_divs)
    clean_text = re.sub(r'\n{3,}', r'\n\n', clean_text).strip()
    return clean_text

def remove_anchor_tags(html_content):
    """Menghapus tag <a> tapi mempertahankan teks di dalamnya."""
    return re.sub(r'<a[^>]*>(.*?)<\/a>', r'\1', html_content)

def sanitize_filename(title):
    """Membersihkan judul agar cocok untuk nama file."""
    clean_title = re.sub(r'[^\w\s-]', '', title).strip().lower()
    return re.sub(r'[-\s]+', '-', clean_title)

def replace_custom_words(text):
    """Menerapkan penggantian kata khusus pada teks."""
    processed_text = text
    sorted_replacements = sorted(REPLACEMENT_MAP.items(), key=lambda item: len(item[0]), reverse=True)
    for old_word, new_word in sorted_replacements:
        pattern = re.compile(re.escape(old_word), re.IGNORECASE)
        processed_text = pattern.sub(new_word, processed_text)
    return processed_text

def edit_first_300_words_with_gemini(post_id, post_title, full_text_content):
    """
    Mengirim 300 kata pertama ke Gemini AI untuk diedit,
    dan menggabungkannya kembali dengan sisa artikel,
    mempertahankan format paragraf sisa artikel.
    """
    words = full_text_content.split()

    if len(words) < 50:
        print(f"[{post_id}] Artikel terlalu pendek (<50 kata) untuk diedit oleh Gemini AI. Melewati pengeditan.")
        return full_text_content

    char_count_for_300_words = 0
    word_count = 0
    
    for i, word in enumerate(words):
        if word_count < 300:
            char_count_for_300_words += len(word)
            if i < len(words) - 1:
                char_count_for_300_words += 1 
            word_count += 1
        else:
            break
            
    char_count_for_300_words = min(char_count_for_300_words, len(full_text_content))

    first_300_words_original_string = full_text_content[:char_count_for_300_words].strip()
    rest_of_article_text = full_text_content[char_count_for_300_words:].strip()

    print(f"🤖 Memulai pengeditan Gemini AI untuk artikel ID: {post_id} - '{post_title}' ({len(first_300_words_original_string.split())} kata pertama)...")

    try:
        prompt = (
            f"Cerita Berikut adalah cuplikan dari 300 kata pertama dari cerita utuhnya, Perbaiki tata bahasa, ejaan, dan tingkatkan keterbacaan paragraf berikut. "
            f"Paraphrase signifikan setiap kata, dan buat agar lebih mengalir sehingga 300 kata pertama ini beda dari aslinya:\n\n"
            f"{first_300_words_original_string}"
        )

        response = gemini_model.generate_content(prompt)
        edited_text_from_gemini = response.text

        print(f"✅ Gemini AI selesai mengedit bagian pertama artikel ID: {post_id}.")

        cleaned_edited_text = strip_html_and_divs(edited_text_from_gemini)

        final_combined_text = cleaned_edited_text.strip() + "\n\n" + rest_of_article_text.strip()

        return strip_html_and_divs(final_combined_text)

    except Exception as e:
        print(f"❌ Error saat mengedit dengan Gemini AI untuk artikel ID: {post_id} - {e}. Menggunakan teks asli untuk bagian ini.")
        return full_text_content

# --- Fungsi untuk memuat dan menyimpan status postingan yang sudah diterbitkan ---
def load_published_posts_state():
    """Memuat ID postingan yang sudah diterbitkan dari file state."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                print(f"Warning: {STATE_FILE} is corrupted or empty. Starting with an empty published posts list.")
                return set()
    return set()

def save_published_posts_state(published_ids):
    """Menyimpan ID postingan yang sudah diterbitkan ke file state."""
    with open(STATE_FILE, 'w') as f:
        json.dump(list(published_ids), f)

# === Inisialisasi Layanan Blogger ===
def get_blogger_service():
    """
    Menginisialisasi dan mengembalikan objek layanan Blogger API.
    Menggunakan Service Account untuk otentikasi dari string JSON di env var.
    """
    creds_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_STRING")
    if not creds_json_str:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS_JSON_STRING environment variable not set.")

    creds_info = json.loads(creds_json_str)

    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/blogger']
    )
    service = build('blogger', 'v3', credentials=creds)
    return service

# === Fungsi untuk Mengirim Post ke Blogger ===
def publish_post_to_blogger(blogger_service, blog_id, title, content_markdown, tags=None):
    """
    Menerbitkan postingan ke Blogger dari konten Markdown.
    """
    print(f"🚀 Menerbitkan '{title}' ke Blogger...")

    # Konversi Markdown ke HTML
    content_html = markdown.markdown(content_markdown)

    # Buat payload postingan
    post_body = {
        'kind': 'blogger#post',
        'blog': {'id': blog_id},
        'title': title,
        'content': content_html,
        'labels': tags if tags else []
    }

    try:
        request = blogger_service.posts().insert(blogId=blog_id, body=post_body)
        response = request.execute()
        print(f"✅ Artikel '{title}' berhasil diterbitkan ke Blogger! URL: {response.get('url')}")
        return response
    except Exception as e:
        print(f"❌ Gagal menerbitkan artikel '{title}' ke Blogger: {e}")
        print(f"Error details: {e.args}") # Tambahkan ini untuk debugging lebih lanjut
        return None

# === Ambil semua postingan dari WordPress Self-Hosted REST API (Diadaptasi dari versi yang kamu bilang berhasil) ===
def fetch_all_and_process_posts():
    """
    Mengambil semua postingan dari WordPress self-hosted REST API, membersihkan HTML,
    dan menerapkan penggantian kata khusus.
    """
    all_posts_raw = []
    page = 1 # Untuk WordPress self-hosted, kita pakai 'page'
    per_page_limit = 100 # Maksimal per halaman

    print("📥 Mengambil semua artikel dari WordPress self-hosted REST API...")

    # Tambahkan headers untuk User-Agent agar lebih "sopan" ke server
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    while True:
        params = {
            'per_page': per_page_limit, # WordPress self-hosted pakai 'per_page'
            'page': page,               # WordPress self-hosted pakai 'page'
            'status': 'publish',        # Hanya ambil yang sudah dipublikasikan
            '_fields': 'id,title,content,excerpt,categories,tags,date,featured_media' # Field yang ingin diambil
        }
        try:
            # Menggunakan timeout untuk mencegah request gantung terlalu lama
            res = requests.get(API_BASE_URL, params=params, headers=headers, timeout=30)
            
            # Cek status code dan pesan error spesifik dari WordPress self-hosted API
            if res.status_code == 400:
                # Ini adalah error yang spesifik dari WP self-hosted jika halaman habis
                if "rest_post_invalid_page_number" in res.text:
                    print(f"Reached end of posts from WordPress API (page {page} does not exist). Stopping fetch.")
                    break # KELUAR DARI LOOP KARENA SEMUA HALAMAN SUDAH DIAMBIL
                else:
                    # Jika 400 tapi bukan invalid_page_number, ini adalah error 400 yang lain
                    raise Exception(f"Error: Gagal mengambil data dari WordPress REST API: {res.status_code} - {res.text}. "
                                    f"Pastikan URL API Anda benar dan dapat diakses.")
            elif res.status_code != 200:
                # Jika status code lain selain 200 (sukses) atau 400 (invalid page), lempar error umum
                raise Exception(f"Error: Gagal mengambil data dari WordPress REST API: {res.status_code} - {res.text}. "
                                f"Pastikan URL API Anda benar dan dapat diakses.")

            posts_batch = res.json()

            if not posts_batch:
                # Ini adalah kondisi standar jika API mengembalikan array kosong di akhir
                print(f"Fetched empty batch on page {page}. Stopping fetch.")
                break

            all_posts_raw.extend(posts_batch)
            page += 1
            time.sleep(0.5) # Beri jeda antar permintaan agar tidak membebani server

        except requests.exceptions.Timeout:
            print(f"Timeout: Permintaan ke WordPress API di halaman {page} habis waktu. Mungkin ada masalah jaringan atau server lambat.")
            break # Berhenti jika ada timeout, bisa coba lagi nanti
        except requests.exceptions.RequestException as e:
            print(f"Network Error: Gagal terhubung ke WordPress API di halaman {page}: {e}. Cek koneksi atau URL.")
            break # Berhenti jika ada masalah jaringan/koneksi

    processed_posts = []
    for post in all_posts_raw:
        original_title = post.get('title', {}).get('rendered', '')
        processed_title = replace_custom_words(original_title)
        post['processed_title'] = processed_title

        raw_content = post.get('content', {}).get('rendered', '')
        content_image_url = extract_first_image_url(raw_content)
        post['content_image_url'] = content_image_url

        content_no_anchors = remove_anchor_tags(raw_content)
        cleaned_formatted_content = strip_html_and_divs(content_no_anchors)
        content_after_replacements = replace_custom_words(cleaned_formatted_content)

        post['raw_cleaned_content'] = content_after_replacements
        post['id'] = post.get('id') # Menggunakan 'id' (huruf kecil) untuk WordPress self-hosted
        processed_posts.append(post)

    return processed_posts

# === Eksekusi Utama ===
if __name__ == '__main__':
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Starting WordPress to Blogger publishing process...")
    print("🚀 Mengambil semua artikel WordPress self-hosted.")
    print("🤖 Fitur Pengeditan 300 Kata Pertama oleh Gemini AI DIAKTIFKAN.")
    print("🖼️ Mencoba mengambil gambar pertama dari konten artikel.")
    print("Directly publishing to Blogger, no local Markdown file generation.")

    try:
        # 1. Muat daftar postingan yang sudah diterbitkan
        published_ids = load_published_posts_state()
        print(f"Ditemukan {len(published_ids)} postingan yang sudah diterbitkan sebelumnya.")

        # 2. Ambil semua postingan dari API WordPress dan lakukan pre-processing
        all_posts_preprocessed = fetch_all_and_process_posts()
        print(f"Total {len(all_posts_preprocessed)} artikel ditemukan dan diproses awal dari WordPress API.")

        # 3. Filter postingan yang belum diterbitkan
        unpublished_posts = [post for post in all_posts_preprocessed if str(post['id']) not in published_ids]
        print(f"Ditemukan {len(unpublished_posts)} artikel yang belum diterbitkan.")

        if not unpublished_posts:
            print("\n🎉 Tidak ada artikel baru yang tersedia untuk diterbitkan hari ini. Proses selesai.")
            exit()

        # 4. Urutkan postingan yang belum diterbitkan dari yang TERBARU
        # Pastikan format tanggal cocok untuk datetime.fromisoformat
        unpublished_posts.sort(key=lambda x: datetime.datetime.fromisoformat(x['date'].replace('Z', '+00:00')), reverse=True)

        # 5. Pilih satu postingan untuk diterbitkan hari ini
        post_to_publish = unpublished_posts[0]

        print(f"🌟 Memproses dan menerbitkan artikel berikutnya: '{post_to_publish.get('processed_title')}' (ID: {post_to_publish.get('id')})")

        # LAKUKAN PENGEDITAN AI
        final_processed_content = edit_first_300_words_with_gemini(
            post_to_publish['id'],
            post_to_publish['processed_title'],
            post_to_publish['raw_cleaned_content']
        )

        # 6. Inisialisasi layanan Blogger
        blogger_service = get_blogger_service()

        # 7. Terbitkan ke Blogger
        if blogger_service and BLOGGER_BLOG_ID:
            publish_post_to_blogger(blogger_service, BLOGGER_BLOG_ID, post_to_publish['processed_title'], final_processed_content)
        else:
            print("Skipping Blogger publishing: BLOGGER_BLOG_ID or Blogger service not configured/initialized.")

        # 8. Tambahkan ID postingan ke daftar yang sudah diterbitkan dan simpan state
        published_ids.add(str(post_to_publish['id'])) # Menggunakan 'id' (huruf kecil)
        save_published_posts_state(published_ids)
        print(f"✅ State file '{STATE_FILE}' diperbarui.")

        print("\n🎉 Proses Selesai! Artikel telah diterbitkan langsung ke Blogger.")

    except Exception as e:
        print(f"❌ Terjadi kesalahan fatal: {e}")
        import traceback
        traceback.print_exc()
