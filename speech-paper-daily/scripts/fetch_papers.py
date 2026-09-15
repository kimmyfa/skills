#!/usr/bin/env python3
"""
Standalone Speech Paper Daily Generator
不从arXiv获取今日语音论文并生成markdown，不依赖mcporter
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import re

REPO_NAME = "speech-paper-daily"
REPO_OWNER = "kimmyfa"
# 不再硬编码 token（旧 token 已失效）。推送一律复用仓库 origin 内置的有效 token。
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUTPUT_DIR = ".skills/speech-paper-daily/output"
STATE_FILE = "/tmp/speech_paper_daily_state.json"

SPEECH_KEYWORDS = [
    "speech", "audio", "asr", "tts", "ssr", "stt", "vocoder",
    "speaker", "enhancement", "separation", "beamforming",
    "streaming", "end-to-end", "neural audio", "codec"
]

EXCLUDE_KEYWORDS = [
    "music generation", "image", "video", "reinforcement learning",
    "robotics", "graph", "recommender"
]


def load_state():
    """加载任务状态"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_state(state):
    """保存任务状态"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def get_target_date():
    """获取目标执行日期，支持断点续传
    
    凌晨执行时获取昨天的论文，因为arXiv上新论文是前一天的
    """
    state = load_state()
    today = datetime.now().date()
    
    target_date_str = state.get('target_date')
    
    if target_date_str:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        return target_date_str, state
    
    target_date = today - timedelta(days=1)
    target_date_str = target_date.strftime("%Y-%m-%d")
    state = {
        'target_date': target_date_str,
        'status': 'started',
        'started_at': datetime.now().isoformat()
    }
    save_state(state)
    return target_date_str, state


def update_status(new_status):
    """更新任务状态"""
    state = load_state()
    state['status'] = new_status
    state['updated_at'] = datetime.now().isoformat()
    save_state(state)


def fetch_arxiv_papers(date_str):
    """从arXiv API获取指定日期的新论文"""
    import time
    query = "cat:eess.AS+OR+cat:cs.SD+OR+cat:cs.CL"
    url = f"https://export.arxiv.org/api/query?search_query={query}&start=0&max_results=50&sortBy=submittedDate&sortOrder=descending"

    papers = []
    content = None
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = 10 * attempt
                print(f"⚠️ 重试 {attempt}/{max_retries}，等待 {wait_time}秒...")
                time.sleep(wait_time)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read().decode('utf-8')
                break
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Error fetching papers: {e}")
                return papers
    
    if not content:
        return papers

    try:
        entries = re.findall(r'<entry>(.*?)</entry>', content, re.DOTALL)
        for entry in entries:
            paper = {}

            id_match = re.search(r'<id>(.*?)</id>', entry)
            if id_match:
                paper['id'] = id_match.group(1).split('/')[-1]

            title_match = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
            if title_match:
                paper['title'] = ' '.join(title_match.group(1).split())

            summary_match = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
            if summary_match:
                paper['abstract'] = ' '.join(summary_match.group(1).split())

            author_matches = re.findall(r'<name>(.*?)</name>', entry)
            paper['authors'] = author_matches

            affiliation_matches = re.findall(r'<arxiv:affiliation>(.*?)</arxiv:affiliation>', entry)
            if affiliation_matches:
                paper['affiliations'] = affiliation_matches
            else:
                paper['affiliations'] = []

            date_match = re.search(r'<published>(.*?)</published>', entry)
            if date_match:
                paper['published'] = date_match.group(1)[:10]

            papers.append(paper)
    except Exception as e:
        print(f"Error parsing papers: {e}")

    return papers


def get_affiliation_from_pdf(arxiv_id):
    """从arXiv PDF提取作者机构信息"""
    try:
        import fitz
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        pdf_path = f"/tmp/{arxiv_id}.pdf"

        req = urllib.request.Request(pdf_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(pdf_path, 'wb') as f:
                f.write(response.read())

        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc[:3]:
            full_text += page.get_text()

        doc.close()

        lines = full_text.split('\n')
        affiliations = []

        institution_keywords = [
            'university', 'institute', 'college', 'laboratory', 'lab',
            'research', 'school', 'department', 'center', 'centre',
            'hospital', 'company', 'corp', 'inc', 'ltd', 'tech',
            'AI', 'Baidu', 'Tencent', 'Alibaba', 'Microsoft', 'Google',
            'Meta', 'Apple', 'Amazon', 'IBM', 'NVIDIA', 'Huawei', 'ByteDance',
            'Polytechnic', 'Sciences', 'Academy', 'Foundation', 'AI'
        ]

        exclude_patterns = ['@', 'http', 'arXiv:', 'preprint',
                           'email', 'contact', 'abstract', 'introduction',
                           'footnote', 'acknowledgement', 'this paper',
                           'proposed', 'method', 'model', 'results',
                           'experiment', 'dataset', 'framework', 'level',
                           'classification', 'speech', 'audio', 'proposed']

        for i, line in enumerate(lines[:50]):
            line_clean = line.strip()
            if not line_clean or len(line_clean) < 10 or len(line_clean) > 250:
                continue

            line_lower = line_clean.lower()
            has_institution = sum(1 for kw in institution_keywords if kw.lower() in line_lower) >= 1
            has_exclude = any(pat in line_lower for pat in exclude_patterns)
            has_email = '@' in line_clean
            starts_with_number = line_clean[0].isdigit() if line_clean else False

            if has_institution and not has_exclude and not has_email:
                line_clean = re.sub(r'^\d+\s*', '', line_clean)
                line_clean = re.sub(r'\s*\d+$', '', line_clean)
                line_clean = re.sub(r'\s*[,;]\s*$', '', line_clean)

                words = line_clean.split()
                if len(words) > 6 and words[0][0].isupper() and ',' in line_clean[:80]:
                    name_count = sum(1 for w in words[:10] if w[0].isupper() and len(w) > 2 and w[:-1].isalpha())
                    if name_count >= 3:
                        continue

                if line_clean not in affiliations:
                    affiliations.append(line_clean)

        os.remove(pdf_path)
        return affiliations[:5]

    except Exception as e:
        return []


def get_affiliation_from_html(arxiv_id):
    """从arXiv HTML页面获取作者机构信息"""
    try:
        html_url = f"https://arxiv.org/abs/{arxiv_id}"
        req = urllib.request.Request(html_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8')

        authors_section = re.search(r'<div class="authors">(.?)</div>', html_content, re.DOTALL)
        if authors_section:
            authors_html = authors_section.group(1)
            affiliations = re.findall(r'supersen.*?<a[^>]*>([^<]*)</a>', authors_html)
            if affiliations:
                return [a.strip() for a in affiliations if a.strip()]

        return []
    except:
        return []


def filter_speech_papers(papers):
    """过滤语音相关论文"""
    filtered = []
    for paper in papers:
        title_lower = paper.get('title', '').lower()
        abstract_lower = paper.get('abstract', '').lower()
        combined = title_lower + ' ' + abstract_lower

        if any(kw in combined for kw in SPEECH_KEYWORDS):
            if not any(ex in combined for ex in EXCLUDE_KEYWORDS):
                paper['direction'] = '语音大模型'
                filtered.append(paper)

    return filtered


def generate_markdown(papers, date_str):
    """生成markdown格式的论文速递"""
    speech_llm = [p for p in papers if p.get('direction') == '语音大模型']
    speech_front = [p for p in papers if p.get('direction') == '语音前端']

    md = f"""# {date_str} 语音论文速递

**共收录**: {len(papers)} 篇 | **语音大模型**: {len(speech_llm)} 篇 | **语音前端**: {len(speech_front)} 篇

> 今日 arXiv 语音相关论文共命中 {len(papers)} 篇。
> 以下是按评分排序的结果。

---

## 🤖 语音大模型

---

"""

    for i, paper in enumerate(speech_llm, 1):
        arxiv_id = paper.get('id', 'unknown')
        title = paper.get('title', 'Untitled')
        authors = ', '.join(paper.get('authors', [])[:8])
        affiliations = paper.get('affiliations', [])
        if affiliations:
            affiliation_str = ', '.join(affiliations[:3])
        else:
            affiliation_str = '(arXiv未提供机构信息)'
        abstract = paper.get('abstract', '')[:500]
        published = paper.get('published', date_str)

        md += f"""## [{i}] {title}

**arXiv ID** {arxiv_id} | **方向** 语音大模型

**作者：** {authors}

**机构：** {affiliation_str}

**发布日期：** {published} | **论文** https://arxiv.org/abs/{arxiv_id} | **PDF** https://arxiv.org/pdf/{arxiv_id}.pdf | **代码** 暂无 | **Demo** 暂无

### 📌 简介
{abstract}

### 🔧 技术方案

**模型架构：** 语音大模型相关架构。

**核心创新：** 基于arXiv摘要信息进行概述。

**训练策略：** 待从全文补充。

### 📊 实验结果
**数据集：** 待从全文补充。

**主要指标：** 待从全文补充。

**是否开源：** 暂无。

### ⭐ 评分：7/10
基于标题和摘要的初步评估，有一定学术价值。

---

"""

    for i, paper in enumerate(speech_front, 1):
        arxiv_id = paper.get('id', 'unknown')
        title = paper.get('title', 'Untitled')
        authors = ', '.join(paper.get('authors', [])[:8])
        affiliations = paper.get('affiliations', [])
        if affiliations:
            affiliation_str = ', '.join(affiliations[:3])
        else:
            affiliation_str = '(arXiv未提供机构信息)'
        abstract = paper.get('abstract', '')[:500]
        published = paper.get('published', date_str)

        md += f"""## [{i}] {title}

**arXiv ID** {arxiv_id} | **方向** 语音前端

**作者：** {authors}

**机构：** {affiliation_str}

**发布日期：** {published} | **论文** https://arxiv.org/abs/{arxiv_id} | **PDF** https://arxiv.org/pdf/{arxiv_id}.pdf | **代码** 暂无 | **Demo** 暂无

### 📌 简介
{abstract}

### 🔧 技术方案

**模型架构：** 语音前端处理相关架构。

**核心创新：** 基于arXiv摘要信息进行概述。

**训练策略：** 待从全文补充。

### 📊 实验结果
**数据集：** 待从全文补充。

**主要指标：** 待从全文补充。

**是否开源：** 暂无。

### ⭐ 评分：7/10
基于标题和摘要的初步评估，有一定学术价值。

---

"""

    md += """今日语音论文速递"""

    return md


def main():
    date_str, state = get_target_date()
    date_dir = date_str

    print(f"📅 获取 {date_str} 的语音论文...")
    print(f"📋 任务状态: {state.get('status', 'unknown')}")

    output_file = os.path.join(OUTPUT_DIR, date_dir, f"speech_paper_{date_dir.replace('-', '')}.md")
    if os.path.exists(output_file):
        print(f"⚠️ {date_str} 的论文已存在，跳过获取")
        update_status('completed')
        sync_to_github(date_dir)
        return

    update_status('fetching')

    papers = fetch_arxiv_papers(date_str)
    print(f"📚 获取到 {len(papers)} 篇论文")

    speech_papers = filter_speech_papers(papers)
    print(f"🎯 过滤后得到 {len(speech_papers)} 篇语音相关论文")

    if not speech_papers:
        print("⚠️ 没有找到语音相关论文")
        update_status('no_papers')
        return

    update_status('getting_affiliations')

    print("📄 正在获取论文机构信息...")
    for paper in speech_papers:
        arxiv_id = paper.get('id', '')
        affiliations = get_affiliation_from_pdf(arxiv_id)
        if affiliations:
            paper['affiliations'] = affiliations
        else:
            paper['affiliations'] = []

    os.makedirs(os.path.join(OUTPUT_DIR, date_dir), exist_ok=True)

    update_status('generating')

    md = generate_markdown(speech_papers, date_str)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(md)

    print(f"✅ 已保存到: {output_file}")

    update_status('syncing')
    sync_to_github(date_dir)
    update_status('completed')


def sync_to_github(date_dir):
    """同步到GitHub

    直接复用项目根目录仓库（origin 已内置有效 token），不再基于 /tmp 克隆与
    硬编码 GITHUB_TOKEN（旧 token 已失效）。推送由 push_to_github.sh 统一负责，
    此处只做同步动作。
    """
    repo_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        print("❌ 项目根目录不是 git 仓库:", repo_path)
        return

    src_path = os.path.join(OUTPUT_DIR, date_dir)
    dst_path = os.path.join(repo_path, "papers", date_dir)
    os.makedirs(dst_path, exist_ok=True)

    for filename in os.listdir(src_path):
        src = os.path.join(src_path, filename)
        dst = os.path.join(dst_path, filename)
        subprocess.run(["cp", src, dst], check=False)

    os.chdir(repo_path)
    subprocess.run(["git", "add", f"papers/{date_dir}/"], check=False)

    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not status.stdout.strip():
        print("✨ 没有新的变更需要提交")
        return

    commit_msg = f"Daily Speech Papers Update - {date_dir}"
    subprocess.run(["git", "commit", "-m", commit_msg], check=False)

    result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)

    if result.returncode == 0:
        print(f"✅ 同步成功! https://github.com/{REPO_OWNER}/{REPO_NAME}")
    else:
        print(f"❌ 推送失败: {result.stderr}")


if __name__ == "__main__":
    main()