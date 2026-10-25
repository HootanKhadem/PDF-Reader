"""Layout-aware text extraction with confidence-scored OCR for damaged scan layers."""
import collections
import csv
import io
import os
import re
import statistics
import subprocess
from pathlib import Path

import pymupdf

VERSION='v4-confidence-columns-cells'
_document=None
_cache_dir=None


def clean(text):
    text=text.replace('\x00','').replace('\u00ad','')
    text=re.sub(r'(\w)-\n(?=[a-z])',r'\1',text)
    return re.sub(r'[ \t]+',' ',text).strip()


def readable(text):
    """Reject symbol/single-letter debris, without a language dictionary."""
    words=re.findall(r'[^\W\d_]+',text,re.UNICODE)
    if not words:
        return False
    # Unsegmented writing systems need no spaces or long words.
    if re.search(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]',text):
        return len(re.sub(r'\s','',text))>=4
    return sum(len(w) for w in words)>=4 and sum(len(w)>=2 for w in words)/len(words)>=.55


def reflow(text):
    text=clean(text)
    lines=text.splitlines()
    # Keep short list/stat rows; reflow ordinary wrapped paragraphs.
    if len(lines)>1 and statistics.median(len(line.split()) for line in lines)>=6:
        text=re.sub(r'(?<!\n)\n(?!\n)',' ',text)
    return text


def ordered_text(blocks,width,height):
    """Group paragraph left edges into columns rather than interleaving rows."""
    blocks=[b for b in blocks if b[4].strip()]
    columns=[]
    for block in sorted(blocks,key=lambda b:b[0]):
        column=next((c for c in columns if abs(block[0]-c[0][0])<width*.115),None)
        if column is None:
            columns.append([block])
        else:
            column.append(block)
    return '\n\n'.join(reflow(b[4]) for column in columns
                       for b in sorted(column,key=lambda b:(b[1],b[0])))


def ocr_blocks(page,cache_path=None):
    if cache_path is not None and cache_path.is_file():
        raw=cache_path.read_bytes()
    else:
        result=subprocess.run(['tesseract','stdin','stdout','-l',os.getenv('OCR_LANGUAGE','eng'),
                               '--psm','3','tsv'],input=page.get_pixmap(dpi=250).tobytes('png'),
            capture_output=True,check=True,timeout=90,
            env={**os.environ,'OMP_THREAD_LIMIT':'1'})
        raw=result.stdout
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True,exist_ok=True)
            temporary=cache_path.with_suffix(f'.{os.getpid()}.tmp')
            temporary.write_bytes(raw)
            temporary.replace(cache_path)
    groups=collections.defaultdict(list)
    for row in csv.DictReader(io.StringIO(raw.decode('utf-8')),delimiter='\t',quoting=csv.QUOTE_NONE):
        if row['level']=='5' and row['text'].strip():
            groups[(row['block_num'],row['par_num'])].append(row)
    blocks=[]
    discarded=0
    confidences=[]
    scale=72/250
    for words in groups.values():
        confidence=statistics.mean(float(w['conf']) for w in words)
        lines=collections.defaultdict(list)
        for word in words:
            lines[word['line_num']].append(word['text'])
        text='\n'.join(' '.join(line) for line in lines.values())
        words_in_text=re.findall(r'[^\W\d_]+',text,re.UNICODE)
        # Short, confidently recognized stat rows are useful even with few letters.
        short_stat=(confidence>=80 and len(text)<=100 and any(c.isdigit() for c in text)
                    and any(len(w)>=2 for w in words_in_text))
        if confidence<55 or not (readable(text) or short_stat):
            discarded+=1
            continue
        confidences.append(confidence)
        x0=min(int(w['left']) for w in words)*scale
        y0=min(int(w['top']) for w in words)*scale
        x1=max(int(w['left'])+int(w['width']) for w in words)*scale
        y1=max(int(w['top'])+int(w['height']) for w in words)*scale
        blocks.append((x0,y0,x1,y1,text))
    return blocks,{'discarded_blocks':discarded,
                   'mean_confidence':round(statistics.mean(confidences),1) if confidences else 0}


def extract(page,ocr_cache=None):
    native=[b[:5] for b in page.get_text('blocks') if b[6]==0]
    text=ordered_text(native,page.rect.width,page.rect.height)
    trace=page.get_texttrace()
    # A single-font invisible layer over a scan is commonly old OCR, not original text.
    scan_layer=bool(trace) and all(t['type']==3 for t in trace) and len({f[3] for f in page.get_fonts()})<=1
    noisy=sum(len(b[4]) for b in native if not readable(b[4]))
    total=sum(len(b[4]) for b in native)
    use_ocr=(os.getenv('FORCE_OCR','0')=='1' or scan_layer or
             len(re.findall(r'\w',text))<50 or noisy/max(total,1)>.15)
    info={'method':'native','discarded_blocks':0}
    if use_ocr:
        blocks,info=ocr_blocks(page,ocr_cache)
        text=ordered_text(blocks,page.rect.width,page.rect.height)
        info['method']='ocr'
    else:
        # Preserve short stat labels and numeric table cells in otherwise usable text.
        # A page with substantial debris already took the confidence-scored OCR path.
        usable=[b for b in native if readable(b[4]) or any(c.isdigit() for c in b[4])
                or re.fullmatch(r'[A-Z]{2,4}',b[4].strip())]
        info['discarded_blocks']=len(native)-len(usable)
        text=ordered_text(usable,page.rect.width,page.rect.height)
    if not readable(text):
        text=''
    return clean(text),use_ocr,info


def init_worker(path,cache_dir=None):
    global _document,_cache_dir
    _document=pymupdf.open(path)
    _cache_dir=Path(cache_dir) if cache_dir is not None else None


def extract_number(index):
    cache=_cache_dir/f'{index}.tsv' if _cache_dir is not None else None
    return index,extract(_document[index],cache)
