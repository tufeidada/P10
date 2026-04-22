"""公司互联网信息监控 — 多源新闻抓取 + LLM 情感分析。

数据源：
1. Tushare 新闻接口 (news)
2. 巨潮资讯网公告 (cninfo via akshare)
3. 东方财富个股新闻 (akshare)

流程：抓取 → 去重 → LLM情感分析+摘要 → 入库 → 重要信息Telegram推送
"""
import os
import sys
import json
import logging
import time
import hashlib
import requests
from datetime import datetime, date
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"), port=5432,
        user=os.getenv("POSTGRES_USER", "hub"),
        password=os.getenv("POSTGRES_PASSWORD", "hub_password_change_me"),
        dbname=os.getenv("POSTGRES_DB", "stock_hub"),
    )


def get_watch_companies(conn) -> list[dict]:
    """获取监控公司列表。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts_code, name, keywords FROM monitor.watch_companies WHERE is_active = true"
        )
        return [{"ts_code": r[0], "name": r[1], "keywords": r[2]} for r in cur.fetchall()]


def fetch_eastmoney_news(stock_name: str, limit: int = 20) -> list[dict]:
    """从东方财富获取个股新闻（via akshare）。"""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=stock_name)
        if df is None or df.empty:
            return []

        results = []
        for _, row in df.head(limit).iterrows():
            results.append({
                "title": str(row.get("新闻标题", "")),
                "content": str(row.get("新闻内容", ""))[:2000],
                "source": "eastmoney",
                "url": str(row.get("新闻链接", "")),
                "pub_date": str(row.get("发布时间", "")),
            })
        return results
    except Exception as e:
        logger.warning("东方财富新闻抓取失败 (%s): %s", stock_name, e)
        return []


def fetch_tushare_news(ts_code: str, start_date: str, end_date: str) -> list[dict]:
    """从 Tushare 获取新闻。"""
    try:
        import tushare as ts
        pro = ts.pro_api(os.getenv("TUSHARE_TOKEN", ""))
        df = pro.news(src="sina", start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return []

        # 过滤包含股票代码/名称的新闻
        results = []
        for _, row in df.iterrows():
            title = str(row.get("title", ""))
            content = str(row.get("content", ""))[:2000]
            results.append({
                "title": title,
                "content": content,
                "source": "tushare_sina",
                "url": "",
                "pub_date": str(row.get("datetime", "")),
            })
        return results
    except Exception as e:
        logger.warning("Tushare新闻抓取失败: %s", e)
        return []


def analyze_sentiment(title: str, content: str) -> dict:
    """用 LLM 分析情感和提取摘要。"""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return {"sentiment": "neutral", "sentiment_score": 0, "summary": "", "is_important": False}

    prompt = f"""分析这条股票新闻的情感和重要性。返回 JSON：
{{"sentiment": "positive/negative/neutral", "score": -1到1的小数, "summary": "一句话摘要", "important": true/false}}

标题: {title}
内容: {content[:500]}"""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("OPENROUTER_MODEL", "xiaomi/mimo-v2-pro"),
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return {
            "sentiment": data.get("sentiment", "neutral"),
            "sentiment_score": float(data.get("score", 0)),
            "summary": data.get("summary", ""),
            "is_important": bool(data.get("important", False)),
        }
    except Exception as e:
        logger.warning("LLM情感分析失败: %s", e)
        return {"sentiment": "neutral", "sentiment_score": 0, "summary": "", "is_important": False}


def dedup_title(conn, title: str) -> bool:
    """检查标题是否已存在。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM monitor.news WHERE title = %s LIMIT 1", (title,))
        return cur.fetchone() is not None


def save_news(conn, ts_code: str, news_list: list[dict]):
    """保存新闻到数据库。"""
    saved = 0
    for item in news_list:
        if dedup_title(conn, item["title"]):
            continue

        # LLM 情感分析
        analysis = analyze_sentiment(item["title"], item.get("content", ""))

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monitor.news
                (ts_code, title, content, source, url, pub_date,
                 sentiment, sentiment_score, summary, is_important)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                ts_code, item["title"], item.get("content"),
                item["source"], item.get("url"),
                item.get("pub_date"),
                analysis["sentiment"], analysis["sentiment_score"],
                analysis["summary"], analysis["is_important"],
            ))
        conn.commit()
        saved += 1

        # 重要新闻推送 Telegram
        if analysis["is_important"]:
            try:
                from watchdog.telegram import send_message
                send_message(
                    f"📰 <b>重要新闻</b> [{ts_code}]\n"
                    f"{item['title']}\n"
                    f"情感: {analysis['sentiment']} ({analysis['sentiment_score']:+.1f})\n"
                    f"{analysis['summary']}"
                )
            except Exception:
                pass

        time.sleep(0.5)  # LLM 限频

    return saved


def run_monitor():
    """执行一轮监控。"""
    conn = get_conn()
    companies = get_watch_companies(conn)

    if not companies:
        logger.info("No companies to monitor. Add via monitor.watch_companies table.")
        conn.close()
        return

    logger.info("Monitoring %d companies", len(companies))

    for company in companies:
        ts_code = company["ts_code"]
        name = company["name"] or ts_code

        logger.info("[%s] %s — fetching news...", ts_code, name)

        # 东方财富新闻
        news = fetch_eastmoney_news(name)
        if news:
            saved = save_news(conn, ts_code, news)
            logger.info("[%s] eastmoney: %d new / %d total", ts_code, saved, len(news))

        time.sleep(1)

    conn.close()
    logger.info("Monitor cycle complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_monitor()
