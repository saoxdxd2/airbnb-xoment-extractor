import asyncio
import json
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# Airbnb review container class (may need update if Airbnb changes the layout)
TARGET_CLASS = "r1are2x1 atm_gq_1vi7ecw dir dir-ltr"

def parse_review_text(text):
    username = None
    time_in_airbnb = None
    rating = None
    post_time = None
    comment = None
    response = None

    if 'Rating,' in text:
        part1, part2 = text.split('Rating,', 1)
        m = re.match(r"(.+?)(\d+ years on Airbnb)", part1.strip())
        if m:
            username = m.group(1).strip()
            time_in_airbnb = m.group(2).strip()
        else:
            splits = part1.strip().split()
            if len(splits) >= 2:
                username = splits[0]
                time_in_airbnb = " ".join(splits[1:])
            else:
                username = part1.strip()

        rating_match = re.search(r"(\d+ stars)", part2)
        if rating_match:
            rating = rating_match.group(1)

        post_time_match = re.search(r"· ([A-Za-z]+ \d{4}) ,", part2)
        if post_time_match:
            post_time = post_time_match.group(1)

        comment_start = part2.find(post_time_match.group(0)) + len(post_time_match.group(0)) if post_time_match else 0
        comment_full = part2[comment_start:].strip(" ·,")
    else:
        splits = text.split(" ", 1)
        username = splits[0]
        comment_full = splits[1] if len(splits) > 1 else ""

    # Split comment and response if present
    response_match = re.search(r"Response from [^\n]+?\d{4} (.+)$", comment_full, re.DOTALL)
    if response_match:
        # Try to extract the response using the marker
        split_point = re.search(r"Response from [^\n]+?\d{4}", comment_full)
        if split_point:
            comment = comment_full[:split_point.start()].strip()
            response = comment_full[split_point.end():].strip()
        else:
            comment = comment_full.strip()
    else:
        comment = comment_full.strip()

    return {
        "username": username,
        "time_in_airbnb": time_in_airbnb,
        "rating": rating,
        "post_time": post_time,
        "comment": comment,
        "response": response
    }

def get_page_id_from_url(url):
    m = re.search(r"/rooms/(\d+)", url)
    return m.group(1) if m else "unknown"

def extract_data_state_json(soup):
    script = soup.find("script", string=re.compile(r"window\.__INITIAL_STATE__\s*=\s*"))
    if not script:
        return {}
    try:
        json_text = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*\});", script.string, re.DOTALL).group(1)
        return json.loads(json_text)
    except Exception as e:
        print(f"Failed to parse __INITIAL_STATE__: {e}")
        return {}

def normalize_name(name):
    return name.lower().strip() if name else ""

def extract_bg_images(el):
    urls = []
    style = el.get("style", "")
    matches = re.findall(r'url\("([^\"]+)"\)', style)
    for url in matches:
        if url.startswith("https://") and url not in urls:
            urls.append(url)
    return urls

async def fetch_and_extract(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"Loading page: {url}")
        await page.goto(url, wait_until="load", timeout=60000)

        selector = "." + ".".join(TARGET_CLASS.split())
        await page.wait_for_selector(selector, timeout=20000)

        # 1) Load all reviews on the DOM
        previous_count = 0
        retries = 0
        load_more_selector = (
            'button[aria-label*=\"Load more reviews\"],' +
            'button[data-testid=\"reviews-load-more-button\"]'
        )
        while True:
            review_els = await page.query_selector_all(selector)
            count = len(review_els)
            print(f"Reviews in DOM: {count}")
            if count == previous_count:
                retries += 1
                if retries >= 5:
                    break
            else:
                retries = 0
                previous_count = count

            btn = await page.query_selector(load_more_selector)
            if btn:
                print(" → clicking Load more…")
                await btn.click()
                await asyncio.sleep(2)
                continue

            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # 2) Process each review individually
        reviews = []
        raw_elements = await page.query_selector_all(selector)
        user_map = {}
        for idx, handle in enumerate(raw_elements, start=1):
            print(f"\n--- Processing review #{idx} ---")
            await handle.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)

            html = await handle.evaluate("el => el.outerHTML")
            soup = BeautifulSoup(html, "html.parser")
            el = soup.select_one(selector)

            text = el.get_text(separator=" ", strip=True)
            parsed = parse_review_text(text)
            parsed["data_review_id"] = el.get("data-review-id")

            image_urls = []
            for img in el.find_all("img", recursive=True):
                src = img.get("src") or img.get("data-original-uri")
                if src and src.startswith("http") and src not in image_urls:
                    image_urls.append(src)
            image_urls.extend(extract_bg_images(el))

            if not image_urls and parsed["username"]:
                if not user_map:
                    full_soup = BeautifulSoup(await page.content(), "html.parser")
                    js = extract_data_state_json(full_soup)
                    for k, v in js.items():
                        if isinstance(v, dict) and v.get("first_name"):
                            pic = v.get("profile_picture") or v.get("picture_url")
                            url = pic.get("picture") if isinstance(pic, dict) else pic
                            if url:
                                user_map[normalize_name(v["first_name"]) ] = url
                fb = user_map.get(normalize_name(parsed["username"]))
                if fb:
                    image_urls.append(fb)

            parsed["images"] = image_urls
            reviews.append(parsed)

        await browser.close()
        return reviews

async def main():
    url = input("Enter Airbnb reviews page URL: ").strip()
    reviews = await fetch_and_extract(url)
    if not reviews:
        print("No reviews extracted.")
        return

    page_id = get_page_id_from_url(url)
    filename = f"airbnb_reviews_{page_id}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

    print(f"\nExtracted {len(reviews)} reviews to {filename}\n")
    for i, r in enumerate(reviews, 1):
        print(f"Review {i}:", json.dumps(r, ensure_ascii=False, indent=2))
        print("-" * 60)

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QPushButton, QLabel, QTextEdit, QFileDialog
    from PyQt5.QtCore import Qt
    import nest_asyncio

    class ExtractorGUI(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Airbnb Review Extractor")
            self.setGeometry(100, 100, 600, 400)
            layout = QVBoxLayout()

            self.url_input = QLineEdit(self)
            self.url_input.setPlaceholderText("Enter Airbnb URL here...")
            layout.addWidget(self.url_input)

            self.run_button = QPushButton("Extract Reviews", self)
            layout.addWidget(self.run_button)

            self.save_button = QPushButton("Save As", self)
            layout.addWidget(self.save_button)
            self.save_button.setEnabled(False)

            self.result_label = QLabel("", self)
            self.result_label.setAlignment(Qt.AlignLeft)
            layout.addWidget(self.result_label)

            self.result_text = QTextEdit(self)
            self.result_text.setReadOnly(True)
            layout.addWidget(self.result_text)

            self.setLayout(layout)
            self.run_button.clicked.connect(self.on_run)
            self.save_button.clicked.connect(self.save_as)

        def on_run(self):
            url = self.url_input.text().strip()
            if not url:
                self.result_label.setText("Please enter a URL.")
                return
            self.result_label.setText("Extracting... Please wait.")
            self.result_text.clear()
            QApplication.processEvents()
            try:
                nest_asyncio.apply()
                import asyncio
                result = asyncio.run(fetch_and_extract(url))
                self.result_label.setText("Extraction complete.")
                self.result_text.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))
                self.save_button.setEnabled(True)
            except Exception as e:
                self.result_label.setText(f"Error: {e}")
                self.save_button.setEnabled(False)

        def save_as(self):
            text = self.result_text.toPlainText()
            if not text.strip():
                self.result_label.setText("Nothing to save.")
                return
            options = QFileDialog.Options()
            options |= QFileDialog.DontUseNativeDialog
            filename, _ = QFileDialog.getSaveFileName(self, "Save Extraction Result", "extraction.json", "JSON Files (*.json);;All Files (*)", options=options)
            if filename:
                try:
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(text)
                    self.result_label.setText(f"Saved to: {filename}")
                except Exception as e:
                    self.result_label.setText(f"Save failed: {e}")

    app = QApplication(sys.argv)
    gui = ExtractorGUI()
    gui.show()
    sys.exit(app.exec_())