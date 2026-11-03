FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --with-deps installs all required system libraries automatically
RUN playwright install --with-deps chromium

COPY MD_General_Scraper.py MD_General_Tracker.html ./

EXPOSE 8765

CMD ["python", "-u", "MD_General_Scraper.py"]
