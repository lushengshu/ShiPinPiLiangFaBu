#!/usr/bin/env python3
"""微信视频号独立批量发布工具本地页面。"""
from __future__ import annotations
import csv, io, json, mimetypes, os, re, shutil, signal, subprocess, sys, threading, time
from email.parser import BytesParser
from email.policy import default as email_policy
import app as douyin_app
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs
ROOT=Path(__file__).resolve().parent; PYTHON=str(ROOT/'.venv/bin/python') if (ROOT/'.venv/bin/python').is_file() else sys.executable; STATE=ROOT/'.channels_batch'; QUEUE=STATE/'queue.json'; PUB=STATE/'publish_state.json'; SHARED=ROOT/'.douyin_batch'/'queue.json'; COLLECTIONS=STATE/'collections.json'; PROFILE=ROOT/'.channels_browser_profile'; PORT=int(os.environ.get('CHANNELS_PORT','8786'))

def shared_items_by_path():
    try:
        rows=json.loads(SHARED.read_text(encoding='utf-8'))
    except Exception:
        return {}
    result={}
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict):
            continue
        try:
            result[str(Path(row.get('path','')).expanduser().resolve())]=row
        except Exception:
            continue
    return result


def item_from_path(path: str, source: dict | None = None, body: dict | None = None) -> dict:
    body = body or {}
    source = source or {}
    def pick(name, default=''):
        return body.get(name) if body.get(name) not in (None, '') else source.get(name, default)
    return {
        'id':path,
        'path':path,
        'name':Path(path).name,
        'title':pick('title') or pick('work_title'),
        'caption':pick('caption'),
        'hashtags':pick('hashtags',''),
        'cover_image_portrait':source.get('cover_image_portrait',''),
        'cover_image_landscape':source.get('cover_image_landscape',''),
        'cover_image':source.get('cover_image',''),
        'cover_image_portrait_name':source.get('cover_image_portrait_name',''),
        'cover_image_landscape_name':source.get('cover_image_landscape_name',''),
        'cover_image_name':source.get('cover_image_name',''),
        'collection_name':body.get('collection_name','') or source.get('channels_collection','') or source.get('collection_name',''),
        'location':body.get('location','') or source.get('location',''),
        'status':'pending',
        'message':'',
    }


def read(p,d):
 try:return json.loads(p.read_text(encoding='utf-8'))
 except:return d
def write(p,v):
 p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
VIDEO_EXTS={".mp4",".mov",".m4v",".webm",".avi",".mkv"}
def choose_folder():
 try:
  out=subprocess.check_output(["osascript","-e","POSIX path of (choose folder with prompt \"选择包含视频的文件夹\")"],text=True,stderr=subprocess.DEVNULL,timeout=300)
  return out.strip().rstrip("/")
 except Exception:
  return ""
def choose_files():
 try:
  script='set theFiles to choose file with prompt "选择视频文件（可多选）" with multiple selections allowed\nset out to ""\nrepeat with f in theFiles\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out'
  out=subprocess.check_output(["osascript","-e",script],text=True,stderr=subprocess.DEVNULL,timeout=300)
  return [line.strip() for line in out.splitlines() if line.strip()]
 except Exception:
  return []


def add_files(paths: list[str]) -> list[dict]:
 items=read(QUEUE,[]); known={str(x.get("path")) for x in items}; shared=shared_items_by_path(); added=[]
 for raw in paths:
  try: path=str(Path(raw).expanduser().resolve())
  except Exception: continue
  if not Path(path).is_file() or Path(path).suffix.lower() not in VIDEO_EXTS or path in known:
   continue
  item=item_from_path(path, shared.get(path), {}); items.append(item); known.add(path); added.append(item)
 if added: write(QUEUE,items)
 return added


def choose_cover_folder():
 try:
  return subprocess.check_output(["osascript","-e","POSIX path of (choose folder with prompt \"选择封面图片所在文件夹\")"],text=True,stderr=subprocess.DEVNULL,timeout=300).strip().rstrip("/")
 except Exception:
  return ""


def image_orientation(path: Path) -> str:
 try:
  out=subprocess.check_output(["sips","-g","pixelWidth","-g","pixelHeight",str(path)],text=True,stderr=subprocess.DEVNULL,timeout=10)
  width=height=0
  for line in out.splitlines():
   if "pixelWidth:" in line: width=int(line.split(":",1)[1].strip())
   if "pixelHeight:" in line: height=int(line.split(":",1)[1].strip())
  if width and height: return "landscape" if width>=height else "portrait"
 except Exception:
  pass
 return douyin_app.cover_orientation(path.name)


def smart_match_covers(folder: str, store_source: bool = False) -> dict:
 items=read(QUEUE, [])
 if not items: return {"error":"视频号队列为空，请先选择视频","matched":[],"unmatched":[]}
 root=Path(folder); covers=douyin_app.COVER_DIR; covers.mkdir(parents=True,exist_ok=True)
 shared=shared_items_by_path()
 files=sorted([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in douyin_app.COVER_EXTENSIONS and not p.name.startswith("._")]) if root.is_dir() else []
 matched=[]; unmatched=[]
 for image in files:
  known=None
  # First use the exact cover filenames already attached to this video in the
  # Douyin queue.  Existing cover files are often named by the capture batch
  # (Volumes_...), so song-name matching alone cannot recognise them.
  for item in items:
   try: source=shared.get(str(Path(str(item.get("path") or "")).resolve()), {})
   except Exception: source={}
   for field,orient in (("cover_image_portrait","portrait"),("cover_image_landscape","landscape")):
    candidates=[source.get(field), item.get(field)]
    for candidate in candidates:
     if candidate and Path(str(candidate)).stem==image.stem:
      known=(item,orient,"与抖音封面文件同名"); break
    if known: break
   if known: break
  if known:
   item,orient,reason=known; score=1.0
  else:
   scored=[]
   for item in items:
    a,ra=douyin_app.cover_match_score(item.get("name"), image.name)
    b,rb=douyin_app.cover_match_score(item.get("path"), image.name)
    score,reason=max((a,ra),(b,rb),key=lambda x:x[0])
    if score>0: scored.append((score,reason,item))
   scored.sort(key=lambda x:x[0],reverse=True)
   if not scored or scored[0][0] < 0.72:
    unmatched.append({"cover":image.name,"reason":"没有找到足够相似的视频文件名"}); continue
   if len(scored)>1 and scored[1][0] >= scored[0][0]-0.08:
    unmatched.append({"cover":image.name,"reason":"多个视频相似度接近，请手动确认"}); continue
   score,reason,item=scored[0]
   orient=image_orientation(image)
  if orient not in ("portrait","landscape"):
   unmatched.append({"cover":image.name,"reason":"无法判断是 3:4 还是 4:3 封面"}); continue
  field="cover_image_portrait" if orient=="portrait" else "cover_image_landscape"
  action="replaced" if str(item.get(field) or "").strip() else "added"
  safe=re.sub(r"[^0-9A-Za-z._-]+","_",Path(str(item.get("path") or item.get("name") or "video")).stem)[:60] or "video"
  cover_stem=re.sub(r"[^0-9A-Za-z._-]+","_",image.stem)[:70] or "cover"
  orient_suffix="" if re.search(r"_(?:portrait|landscape)$",cover_stem,re.I) else f"_{orient}"
  target=covers/f"{safe}__{cover_stem}{orient_suffix}{image.suffix.lower()}"
  old_path=str(item.get(field) or "")
  try:
   if image.resolve()!=target.resolve(): shutil.copy2(image,target)
  except Exception:
   if image.resolve()!=target.resolve(): raise
  if old_path and old_path!=str(target.relative_to(ROOT/'.douyin_batch')) and "_channels_" in old_path:
   try: (ROOT/'.douyin_batch'/old_path).unlink()
   except Exception: pass
  item[field]=str(target.relative_to(ROOT/'.douyin_batch'))
  item[field+"_name"]=image.name
  if store_source:
   item[field+"_source"]=str(image.resolve())
  item['updated_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
  matched.append({"video":item.get('name'),"cover":image.name,"orientation":orient,"score":round(score,3),"reason":reason,"action":action})
 if matched: write(QUEUE,items)
 return {"matched":matched,"unmatched":unmatched}

def add_folder(folder):
 items=read(QUEUE,[]); known={str(x.get("path")) for x in items}; added=[]; shared=shared_items_by_path()
 for root,_,files in os.walk(folder):
  for name in sorted(files):
   if name.startswith('.') or name.startswith('._'):
    continue
   path=str(Path(root)/name)
   if Path(path).suffix.lower() not in VIDEO_EXTS or path in known: continue
   item=item_from_path(path, shared.get(str(Path(path).resolve())), {})
   items.append(item); known.add(path); added.append(item)
 write(QUEUE,items); return added



def find_item(items, item_id):
    for item in items:
        if str(item.get('id') or item.get('path')) == item_id:
            return item
    return None


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def reset_publish_control_flags(pub: dict, total: int = 0) -> dict:
    """复位上一轮终止/暂停留下的控制位。

    终止会把 terminated / stop_requested 写进状态文件；发布器启动后先检查
    这两个标记，如果残留就会在第一条视频开始前直接判定终止并退出，用户看到
    的就是“点了开始发布却没有任何动作”。
    """
    pub.update({'running': False, 'paused': False, 'pause_requested': False,
                'stop_requested': False, 'terminated': False, 'status': 'starting',
                'current': '', 'completed': 0, 'total': total, 'stage': 0,
                'stage_percent': None, 'message': '正在启动视频号发布'})
    pub.pop('pid', None)
    return pub


def stop_profile_chrome():
    try:
        out=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True,timeout=10,check=False).stdout
    except Exception:
        return
    for line in out.splitlines():
        line=line.strip()
        if not line: continue
        pid_text,_,cmd=line.partition(' ')
        if f'--user-data-dir={PROFILE}' not in cmd: continue
        try: os.kill(int(pid_text),signal.SIGTERM)
        except Exception: pass
    for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
        try: (PROFILE/name).unlink()
        except Exception: pass


def choose_cover_file(kind: str, item: dict) -> str:
    prompt = '选择视频号竖版封面（3:4）' if kind == 'portrait' else '选择视频号横版封面（4:3）'
    try:
        out=subprocess.check_output(['osascript','-e',f'POSIX path of (choose file with prompt "{prompt}")'],text=True,stderr=subprocess.DEVNULL,timeout=300).strip()
    except Exception:
        return ''
    if not out: return ''
    src=Path(out)
    if not src.is_file(): return ''
    covers=ROOT/'.douyin_batch'/'covers'; covers.mkdir(parents=True,exist_ok=True)
    safe=re.sub(r'[^0-9A-Za-z\u4e00-\u9fff._-]+','_',Path(item.get('name','cover')).stem)[:60]
    target=covers/f'{safe}_channels_{kind}{src.suffix.lower()}'
    counter=1
    while target.exists():
        target=covers/f'{safe}_channels_{kind}_{counter}{src.suffix.lower()}'; counter+=1
    shutil.copy2(src,target)
    return str(target.relative_to(ROOT/'.douyin_batch'))




def parse_upload(handler, length: int):
    raw=handler.rfile.read(length)
    ctype=handler.headers.get("Content-Type", "")
    msg=BytesParser(policy=email_policy).parsebytes(("Content-Type: "+ctype+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+raw)
    for part in msg.walk():
        filename=part.get_filename()
        if filename:
            return part.get_payload(decode=True) or b"", filename
    return b"", ""


def parse_uploads(handler, length: int):
    raw=handler.rfile.read(length)
    ctype=handler.headers.get("Content-Type", "")
    msg=BytesParser(policy=email_policy).parsebytes(("Content-Type: "+ctype+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+raw)
    files=[]
    for part in msg.walk():
        filename=part.get_filename()
        if filename:
            files.append((filename, part.get_payload(decode=True) or b""))
    return files


def parse_copy_rows(data: bytes, filename: str) -> list[dict]:
    suffix=Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return douyin_app._parse_caption_xlsx(data)
    if suffix == ".json":
        payload=json.loads(data.decode("utf-8-sig"))
        if isinstance(payload, dict): payload=payload.get("rows") or payload.get("items") or []
        return [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
    if suffix == ".csv":
        text=data.decode("utf-8-sig")
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    raise ValueError("仅支持 xlsx、csv、json 文案文件")


def apply_copy_rows(rows: list[dict], overwrite: bool = True) -> dict:
    items=read(QUEUE, [])
    by_name={str(Path(str(x.get("path") or "")).name): x for x in items}
    matched=0; skipped=[]
    for row in rows:
        name=str(row.get("name") or row.get("文件名") or "").strip()
        target=by_name.get(name)
        if target is None:
            stem=Path(name).stem
            target=next((x for x in items if Path(str(x.get("path") or "")).stem==stem), None)
        if target is None:
            if name: skipped.append(name)
            continue
        title=str(row.get("title") or row.get("标题") or "").strip()
        caption=str(row.get("caption") or row.get("文案") or "").strip()
        tags=row.get("hashtags") if row.get("hashtags") is not None else row.get("话题")
        changed=False
        if title and (overwrite or not str(target.get("title") or "").strip()):
            target["title"]=title; changed=True
        if caption and (overwrite or not str(target.get("caption") or "").strip()):
            target["caption"]=caption; changed=True
        if tags is not None and (overwrite or not str(target.get("hashtags") or "").strip()):
            target["hashtags"]=tags; changed=True
        if changed:
            target["updated_at"]=time.strftime('%Y-%m-%dT%H:%M:%S%z'); matched+=1
        else:
            skipped.append(name or "未命名行")
    if matched: write(QUEUE,items)
    return {"ok":True,"matched":matched,"skipped":skipped,"total":len(rows)}




def generate_channels_copy(ids=None, overwrite: bool = False) -> dict:
    """Generate Channels title/caption/topics from the shared song copy library."""
    try:
        library=douyin_app.load_copy_library()
    except Exception as exc:
        return {"error":str(exc)}
    index=douyin_app._copy_library_index(library)
    items=read(QUEUE,[])
    requested={str(x) for x in (ids or []) if str(x)}
    generated=[];skipped=[];unmatched=[];changed=False
    for item in items:
        item_id=str(item.get("id") or item.get("path") or "")
        if requested and item_id not in requested: continue
        if not requested and str(item.get("status") or "") in {"skipped","published"}: continue
        entry=None
        for candidate in (item.get("name"), item.get("path")):
            entry=index.get(douyin_app.normalize_copy_key(candidate))
            if entry: break
        if entry is None:
            unmatched.append({"id":item_id,"name":item.get("name") or item_id,"reason":"文案库中没有匹配的歌名"}); continue
        title=str(entry.get("title") or "");caption=str(entry.get("caption") or "");tags=douyin_app._copy_entry_tags(library,entry)
        existing_title=str(item.get("title") or item.get("work_title") or "").strip()
        existing_caption=str(item.get("caption") or "").strip()
        existing_tags=douyin_app._normalize_copy_tags(item.get("hashtags"))
        matches=(existing_title==title.strip() and existing_caption==caption.strip() and existing_tags==douyin_app._normalize_copy_tags(tags))
        if not overwrite and (existing_title or existing_caption or existing_tags) and not matches:
            skipped.append({"id":item_id,"name":item.get("name") or item_id,"reason":"已有手写文案"}); continue
        item["title"]=title
        item["work_title"]=title
        item["caption"]=caption
        item["hashtags"]=tags
        item["updated_at"]=time.strftime('%Y-%m-%dT%H:%M:%S%z')
        generated.append({"id":item_id,"name":item.get("name") or item_id,"song":str(entry.get("song") or "")})
        changed=True
    backup=""
    if changed:
        if overwrite and QUEUE.exists():
            backup=str(STATE/f"queue.before_copy_{time.strftime('%Y%m%d_%H%M%S')}.json")
            shutil.copy2(QUEUE,backup)
        write(QUEUE,items)
    message=f"已生成 {len(generated)} 条视频号文案"
    if skipped: message+=f"，保留手写 {len(skipped)} 条"
    if unmatched: message+=f"，{len(unmatched)} 条未匹配文案库"
    return {"ok":True,"generated":generated,"skipped":skipped,"unmatched":unmatched,"backup":backup,"message":message}


class H(BaseHTTPRequestHandler):
 def j(self,v,code=200):
  b=json.dumps(v,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  u=urlparse(self.path).path
  if u=='/api/state':
   pub=read(PUB,{'running':False,'message':''})
   if pub.get('running'):
    pid=int(pub.get('pid') or 0)
    if pid and not pid_alive(pid):
     # 发布器异常退出时会留下 running=true，页面按钮会一直禁用；
     # 这里按真实进程状态自愈，让用户可以重新开始。
     pub.update({'running':False,'paused':False,'status':'idle','message':'上一次发布进程已结束，可以重新开始发布'})
     pub.pop('pid',None); write(PUB,pub)
   return self.j({'items':read(QUEUE,[]),'publish':pub,'collections':read(COLLECTIONS,{'collections':[]})})
  if u=='/':
   b=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  self.send_error(404)
 def do_POST(self):
  u=urlparse(self.path).path; n=int(self.headers.get('Content-Length','0'))
  if u=='/api/copy-generate':
   try:
    body=json.loads(self.rfile.read(n) or '{}')
   except Exception:
    return self.j({'error':'请求数据格式不正确'},400)
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能生成文案'},409)
   ids=body.get('ids') if isinstance(body,dict) and isinstance(body.get('ids'),list) else None
   result=generate_channels_copy(ids,bool(body.get('overwrite')) if isinstance(body,dict) else False)
   return self.j(result,400 if result.get('error') else 200)
  if u=='/api/caption-template':
   try:
    body=json.loads(self.rfile.read(n) or '{}')
   except Exception:
    return self.j({'error':'请求数据格式不正确'},400)
   mapped=[]
   for item in read(QUEUE,[]):
    mapped.append({'name':item.get('name',''),'work_title':item.get('title') or item.get('work_title',''),'caption':item.get('caption',''),'hashtags':item.get('hashtags','')})
   try:
    payload=douyin_app._generate_caption_template(mapped)
   except Exception as exc:
    return self.j({'error':f'生成模板失败：{exc}'},500)
   self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Disposition','attachment; filename="channels-copy-template.xlsx"');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
  if u=='/api/import-copy':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能导入文案'},409)
   data,filename=parse_upload(self,n)
   if not data: return self.j({'error':'没有读取到文案文件'},400)
   try:
    overwrite=parse_qs(urlparse(self.path).query).get('overwrite',['1'])[0] not in ('0','false','False')
    rows=parse_copy_rows(data,filename)
    return self.j(apply_copy_rows(rows, overwrite))
   except Exception as exc:
    return self.j({'error':f'导入文案失败：{exc}'},400)
  if u=='/api/covers/import':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能导入封面'},409)
   uploads=parse_uploads(self,n)
   if not uploads: return self.j({'error':'没有读取到封面图片'},400)
   tmp=STATE/'cover_upload'; tmp.mkdir(parents=True,exist_ok=True)
   try:
    for old in tmp.iterdir():
     try: old.unlink()
     except Exception: pass
    valid=0
    for filename,data in uploads:
     name=Path(str(filename or '')).name
     if not data or name.startswith('._'): continue
     if Path(name).suffix.lower() not in douyin_app.COVER_EXTENSIONS: continue
     (tmp/name).write_bytes(data); valid+=1
    if not valid: return self.j({'error':'没有有效的 JPG/PNG/WEBP 封面图片'},400)
    result=smart_match_covers(str(tmp), store_source=False)
    if result.get('error'): return self.j(result,400)
    result['total']=len(uploads)
    return self.j(result)
   except Exception as exc:
    return self.j({'error':f'封面匹配失败：{exc}'},500)
   finally:
    for old in tmp.iterdir():
     try: old.unlink()
     except Exception: pass
    try: tmp.rmdir()
    except Exception: pass
  try:
   body=json.loads(self.rfile.read(n) or '{}')
  except Exception:
   return self.j({'error':'请求数据格式不正确'},400)
  items=read(QUEUE,[])
  if u=='/api/select-folder':
   folder=choose_folder()
   if not folder: return self.j({'cancelled':True,'added':[]})
   added=add_folder(folder); return self.j({'folder':folder,'added':added,'count':len(added)})
  if u=='/api/select-files':
   paths=choose_files()
   if not paths: return self.j({'cancelled':True,'added':[],'count':0})
   added=add_files(paths)
   return self.j({'added':added,'count':len(added)})
  if u=='/api/items':
   path=str(Path(body.get('path','')).expanduser().resolve())
   if not Path(path).is_file(): return self.j({'error':'文件不存在'},400)
   source=shared_items_by_path().get(str(Path(path).resolve()),{})
   item=item_from_path(path, source, body); items.append(item); write(QUEUE,items); return self.j(item)
  if u=='/api/items/clear': write(QUEUE,[]); return self.j({'ok':True})
  if u=='/api/publish':
   pub=read(PUB,{})
   if pub.get('running'):
    # running 但发布器进程已经不在（崩溃/被杀）时视为残留状态，允许重新开始。
    pid=int(pub.get('pid') or 0)
    if pid and pid_alive(pid): return self.j({'error':'发布任务已在运行'},409)
   eligible=[x for x in items if str(x.get('status') or 'pending') in ('pending','failed','publishing')]
   if not eligible: return self.j({'error':'没有待发布的视频：请把视频状态改为待发布，或重新加入视频'},400)
   # 上一轮点过“终止”会留下终止标记，启动前必须复位，否则发布器会在
   # 第一条开始前直接退出。
   reset_publish_control_flags(pub, len(eligible))
   write(PUB,pub)
   try:
    proc=subprocess.Popen([PYTHON,str(ROOT/'channels_publisher.py'),'--queue'],cwd=ROOT,start_new_session=True)
   except OSError as exc:
    pub.update({'running':False,'status':'idle','message':f'发布器启动失败：{exc}'}); write(PUB,pub)
    return self.j({'error':f'发布器启动失败：{exc}'},500)
   pub.update({'running':True,'pid':proc.pid,'message':'正在启动视频号浏览器'}); write(PUB,pub)
   return self.j({'ok':True,'count':len(eligible)})
  if u=='/api/open-login':
   subprocess.Popen([PYTHON,str(ROOT/'channels_publisher.py'),'--check'],cwd=ROOT,start_new_session=True); return self.j({'ok':True})
  if u=='/api/covers/smart-import':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能匹配封面'},409)
   folder=choose_cover_folder()
   if not folder: return self.j({'cancelled':True,'matched':[],'unmatched':[]})
   return self.j(smart_match_covers(folder, store_source=True))
  if u=='/api/check-env':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能检查环境'},409)
   try:
    proc=subprocess.run([PYTHON,str(ROOT/'channels_publisher.py'),'--check'],cwd=ROOT,capture_output=True,text=True,timeout=120)
    payload=None
    for line in reversed(proc.stdout.splitlines()):
     line=line.strip()
     if line.startswith('{'):
      try: payload=json.loads(line); break
      except Exception: continue
    if not payload: return self.j({'error':'检查失败：'+(proc.stderr or proc.stdout)[-300:]},500)
    return self.j(payload)
   except Exception as exc:
    return self.j({'error':f'检查失败：{exc}'},500)
  if u=='/api/collections/refresh':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能读取合集'},409)
   try:
    proc=subprocess.run([PYTHON,str(ROOT/'channels_publisher.py'),'--list-collections'],cwd=ROOT,capture_output=True,text=True,timeout=120)
    payload=None
    for line in reversed(proc.stdout.splitlines()):
     line=line.strip()
     if line.startswith('{'):
      try: payload=json.loads(line); break
      except Exception: continue
    if not payload: return self.j({'error':'读取合集失败：'+(proc.stderr or proc.stdout)[-300:]},500)
    data={'updated_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'collections':payload.get('collections',[])}
    write(COLLECTIONS,data); return self.j(data)
   except Exception as exc:
    return self.j({'error':f'读取合集失败：{exc}'},500)
  if u=='/api/pause':
   pub=read(PUB,{})
   if not pub.get('running'): return self.j({'error':'当前没有运行中的视频号任务'},409)
   pub.update({'pause_requested':True,'message':'已请求暂停：当前视频完成后暂停'})
   write(PUB,pub); return self.j(pub)
  if u=='/api/resume':
   pub=read(PUB,{})
   if not pub.get('running'): return self.j({'error':'当前没有运行中的视频号任务'},409)
   pub.update({'paused':False,'pause_requested':False,'message':'继续视频号发布'})
   write(PUB,pub); return self.j(pub)
  if u=='/api/terminate':
   pub=read(PUB,{})
   pid=int(pub.get('pid') or 0)
   pub.update({'stop_requested':True,'message':'正在终止视频号发布'}); write(PUB,pub)
   if pid:
    try: os.kill(pid,signal.SIGTERM)
    except Exception: pass
    for _ in range(20):
     if not pid_alive(pid): break
     time.sleep(0.2)
    if pid_alive(pid):
     try: os.kill(pid,signal.SIGKILL)
     except Exception: pass
   stop_profile_chrome()
   current=str(pub.get('current') or '')
   if current:
    for item in items:
     if str(item.get('name') or '')==current:
      item['status']='failed'; item['message']='视频号发布已终止，可重新发布'; write(QUEUE,items); break
   pub.update({'running':False,'paused':False,'stop_requested':False,'terminated':True,'current':'','message':'视频号发布已终止，可重新开始'})
   pub.pop('pid',None); write(PUB,pub); return self.j({'ok':True,'state':pub})
  if u=='/api/items/bulk-collection':
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能批量设置合集'},409)
   ids=body.get('ids') if isinstance(body.get('ids'),list) else []
   name=str(body.get('collection_name') or '').strip()
   if not ids: return self.j({'error':'请先勾选视频'},400)
   by_id={str(x.get('id') or x.get('path')):x for x in items}
   changed=0
   for item_id in ids:
    item=by_id.get(str(item_id))
    if not item: continue
    item['collection_name']=name
    item['collection_id']=str(body.get('collection_id') or '').strip() if name else ''
    item['updated_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
    changed+=1
   if changed: write(QUEUE,items)
   return self.j({'ok':True,'count':changed,'collection_name':name})
  if u.startswith('/api/items/') and u.endswith('/status'):
   pub=read(PUB,{})
   if pub.get('running'): return self.j({'error':'发布任务运行中，暂不能修改视频状态'},409)
   item_id=unquote(u[len('/api/items/'):-len('/status')].strip('/'))
   item=find_item(items,item_id)
   if not item: return self.j({'error':'没有找到这条视频'},404)
   status=str(body.get('status') or '').strip()
   if status not in ('pending','published','failed','skipped'):
    return self.j({'error':'视频状态无效'},400)
   old=str(item.get('status') or 'pending')
   item['status']=status
   item['message']={'pending':'手动改为待发布，将重新进入发布队列','published':'手动标记为已提交','failed':'手动标记为失败待处理','skipped':'已跳过，不参与发布'}.get(status,'')
   item['updated_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
   write(QUEUE,items)
   return self.j({'ok':True,'status':status,'old_status':old,'item':item})
  if u.startswith('/api/items/') and u.endswith('/cover'):
   item_id=unquote(u[len('/api/items/'):-len('/cover')].strip('/'))
   item=find_item(items,item_id)
   if not item: return self.j({'error':'没有找到这条视频'},404)
   kind=str(body.get('kind') or '').strip().lower()
   if kind not in ('portrait','landscape'): return self.j({'error':'封面类型无效'},400)
   rel=choose_cover_file(kind,item)
   if not rel: return self.j({'cancelled':True})
   field='cover_image_portrait' if kind=='portrait' else 'cover_image_landscape'
   item[field]=rel
   item[field+'_name']=Path(rel).name
   write(QUEUE,items); return self.j(item)
  if u.startswith('/api/items/'):
   item_id=unquote(u[len('/api/items/'):].strip('/'))
   item=find_item(items,item_id)
   if not item: return self.j({'error':'没有找到这条视频'},404)
   for key in ('title','caption','hashtags','collection_name','collection_id','location','schedule_mode','schedule_at','cover_image_portrait','cover_image_landscape'):
    if key in body:
     item[key]=body[key] if key in ('hashtags',) else str(body[key] or '').strip()
   write(QUEUE,items); return self.j(item)
  self.send_error(404)
HTML='''<!doctype html><meta charset="utf-8"><title>视频号发布</title><link rel="stylesheet" href="http://127.0.0.1:8765/style.css">
<style>
.channel-link{display:inline-flex;align-items:center;min-height:34px;padding:6px 10px;border:1px solid rgba(255,255,255,.14);border-radius:11px;color:#d1fae5;text-decoration:none;font-size:12px;font-weight:650;background:rgba(255,255,255,.07)}
.channel-link:hover{color:#fff;background:rgba(255,255,255,.15)}
.panel,.card{background:#fff;border:1px solid #e5e6eb;border-radius:14px;padding:16px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:1px solid #c9cdd4;background:#fff;border-radius:8px;padding:9px 13px;font:inherit;cursor:pointer}
button.primary{background:#07c160;border-color:#07c160;color:#fff;font-weight:700}
button.danger{color:#d93026;border-color:#f3b5b0}
button:disabled{opacity:.45;cursor:not-allowed}
input,textarea,select{border:1px solid #c9cdd4;border-radius:8px;padding:9px 10px;font:inherit;background:#fff}
input[type=text],select{min-width:180px}textarea{width:100%;min-height:70px;box-sizing:border-box}
.card{position:relative;padding:16px;overflow:hidden;border:1px solid var(--line);border-radius:18px;background:var(--queue-surface,#fff);box-shadow:0 7px 18px rgba(15,23,42,.055)}
.card::before{content:'';display:block;height:4px;margin:-16px -16px 12px;background:linear-gradient(90deg,var(--queue-accent,#6366f1),var(--queue-line,#a5b4fc))}
.card[class*="queue-tone-"]{border-color:var(--queue-line)}
.card[class*="queue-tone-"]::before{background:linear-gradient(90deg,var(--queue-accent),var(--queue-line))}
.card.published{border-color:#86efac;background:#fbfffd}
.card.published::before{background:linear-gradient(90deg,#16a34a,#86efac)}
.card.queue-tone-0{--queue-surface:#fff5f6;--queue-line:#fecdd3;--queue-accent:#fb7185}
.card.queue-tone-1{--queue-surface:#f2fcf5;--queue-line:#bbf7d0;--queue-accent:#4ade80}
.card.queue-tone-2{--queue-surface:#fffbeb;--queue-line:#fde68a;--queue-accent:#fbbf24}
.card.queue-tone-3{--queue-surface:#f2f7ff;--queue-line:#bfdbfe;--queue-accent:#60a5fa}
.card.queue-tone-4{--queue-surface:#f7f5ff;--queue-line:#ddd6fe;--queue-accent:#a78bfa}
.card.queue-tone-5{--queue-surface:#effcfd;--queue-line:#a5f3fc;--queue-accent:#22d3ee}
.card.queue-tone-6{--queue-surface:#fff7ed;--queue-line:#fed7aa;--queue-accent:#fb923c}
.card.queue-tone-7{--queue-surface:#effcf9;--queue-line:#99f6e4;--queue-accent:#2dd4bf}
.card.queue-tone-8{--queue-surface:#fdf4fa;--queue-line:#fbcfe8;--queue-accent:#f472b6}
.card.queue-tone-9{--queue-surface:#f7fdea;--queue-line:#d9f99d;--queue-accent:#a3e635}
.item-head{display:flex;justify-content:flex-start;gap:10px;align-items:flex-start}
.item-head .status{margin-left:auto;flex:none}
.status-select{margin-left:auto;flex:none;min-width:108px;min-height:30px;padding:4px 7px;border:1px solid #dbe3ef;border-radius:8px;background:#fff;color:#334155;font-size:12px;font-weight:700}
.status-select:focus{outline:2px solid rgba(7,193,96,.3);outline-offset:1px}
.item-head b{font-size:16px}.status{font-size:12px;background:#eef1f4;border-radius:999px;padding:3px 9px;color:#4e5969}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.fields label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:#4e5969}
.fields .wide{grid-column:1/-1}
.cover-import-box{padding:0 18px 10px}
.cover-import-hint{display:block;color:#64748b;font-size:12px;line-height:1.6}
.cover-import-hint.is-error{color:#b91c1c}
.cover-import-result{margin-top:7px;padding:10px 12px;border:1px solid #dbe3ef;border-radius:10px;background:#f8fafc;font-size:12px;max-height:180px;overflow:auto}
.cover-import-result strong{display:block;margin-bottom:4px;color:#334155}
.cover-import-result ul{margin:4px 0 0 17px;padding:0;color:#475569;line-height:1.65}
.cover-import-result .ok{color:#047857}.cover-import-result .bad{color:#b91c1c}
.publish-progress{margin:0 0 14px;padding:13px 16px;border:1px solid #99f6e4;border-radius:14px;background:#f0fdfa;color:#0f766e;box-shadow:0 6px 18px rgba(30,41,59,.05)}
.publish-progress[hidden]{display:none}
.progress-head{display:flex;justify-content:space-between;gap:12px;align-items:center;font-size:13px}
.progress-head span{font-size:12px;font-weight:700;color:#0f766e}
.progress-track{position:relative;height:8px;margin-top:8px;overflow:hidden;border-radius:999px;background:#ccfbf1}
.progress-track.is-busy::after{content:"";position:absolute;top:0;bottom:0;left:0;width:36%;border-radius:999px;background:linear-gradient(90deg,transparent,#10b981,transparent);animation:progressSweep 1.3s linear infinite}
@keyframes progressSweep{0%{transform:translateX(-110%)}100%{transform:translateX(320%)}}
.progress-fill{height:100%;width:0;border-radius:999px;background:linear-gradient(90deg,#06b6d4,#10b981);transition:width .35s ease}
.progress-message{margin-top:7px;color:#0f766e;font-size:12px;line-height:1.5}
.copy-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:0 18px 10px}
.copy-tools button{min-height:34px;padding:6px 10px;font-size:12px}
.copy-overwrite{display:inline-flex;gap:5px;align-items:center;color:#64748b;font-size:12px}
.copy-hint{color:#64748b;font-size:12px}
.copy-hint.is-error{color:#b91c1c}
.copy-result{margin:0 18px 12px;padding:10px 12px;border:1px solid #dbe3ef;border-radius:10px;background:#f8fafc;font-size:12px;max-height:160px;overflow:auto}
.item-select{display:flex;align-items:center;flex:none;margin-top:2px}
.item-select input{width:17px;height:17px;accent-color:#07c160;cursor:pointer}
.batch-collection{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 18px 12px;padding:10px 12px;border:1px solid #d1fae5;border-radius:10px;background:#f0fdf4;font-size:12px}
.batch-collection select{min-width:180px}
.batch-collection button{min-height:34px;padding:6px 11px;font-size:12px}
.batch-select-all{display:inline-flex;gap:5px;align-items:center;color:#047857;font-weight:700}
.batch-count{color:#047857;min-width:64px}
.copy-result ul{margin:4px 0 0 17px;padding:0;color:#475569;line-height:1.65}
.cover-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px;color:#4e5969}
.cover-label{font-weight:700;color:#64748b}
.cover-name{background:#f2f3f5;border-radius:6px;padding:6px 9px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}
#msg{color:#4e5969}.hint{color:#86909c;font-size:12px}
@media(max-width:720px){.fields{grid-template-columns:1fr}header{flex-direction:column}}
</style>
<main class="shell">
<header class="hero">
 <div class="hero-copy">
  <div class="brand-line"><span class="brand-mark">✺</span><p class="eyebrow">LOCAL CREATOR TOOL</p><span class="local-chip"><i></i> 本机运行</span></div>
  <h1>视频号发布助手</h1>
  <p class="subtitle">独立队列、独立登录态；上传视频后自动完成双封面、标题、描述、视频标注、合集和发表。</p>
 </div>
 <div class="hero-console">
  <div class="console-label"><span class="console-pulse"></span>视频号发布</div>
  <div class="actions">
   <div class="action-row action-row-primary">
    <button class="quiet-button" id="collectionsBtn">读取视频号合集</button>
    <button class="quiet-button" id="checkBtn">检查发布环境</button>
    <button class="channels-publish-all" id="publishBtn">开始发布</button>
    <button class="quiet-button" id="pauseBtn" hidden>暂停视频号</button>
    <button class="quiet-button" id="resumeBtn" hidden>继续视频号</button>
    <button class="stop-publish" id="stopBtn" hidden>终止视频号</button>
   </div>
   <div class="action-row action-row-utility">
    <a class="channel-link" href="http://127.0.0.1:8765/" target="_blank" rel="noopener">← 切换平台</a>
    <button class="quiet-button" id="clearBtn">清空队列</button>
   </div>
   <span id="msg" class="action-hint"></span>
  </div>
 </div>
</header>
<nav class="workflow-steps" aria-label="视频号发布流程">
 <div class="workflow-step active" data-step="1"><span>01</span><div><b>选择视频</b><small>独立队列</small></div></div><span class="workflow-line"></span>
 <div class="workflow-step" data-step="2"><span>02</span><div><b>编辑内容</b><small>短标题、描述、话题</small></div></div><span class="workflow-line"></span>
 <div class="workflow-step" data-step="3"><span>03</span><div><b>匹配封面</b><small>3:4 / 4:3</small></div></div><span class="workflow-line"></span>
 <div class="workflow-step" data-step="4"><span>04</span><div><b>选择合集</b><small>读取后逐条配置</small></div></div><span class="workflow-line"></span>
 <div class="workflow-step" data-step="5"><span>05</span><div><b>确认发布</b><small>标注后发表</small></div></div>
</nav>
<div class="workspace-layout"><div class="main-column">
<section class="stats">
 <div class="stat"><b id="statPending">0</b><span>待发布</span></div>
 <div class="stat"><b id="statPublished">0</b><span>已提交</span></div>
 <div class="stat"><b id="statFailed">0</b><span>失败待处理</span></div>
 <div class="stat"><b id="statCollections">0</b><span>可用合集</span></div>
 <div class="stat"><b id="statTotal">0</b><span>总视频</span></div>
</section>
<section id="publishProgress" class="publish-progress" hidden>
 <div class="progress-head"><strong id="progressTitle">视频号发布</strong><span id="progressCount"></span></div>
 <div class="progress-track"><div id="progressFill" class="progress-fill"></div></div>
 <div id="progressMessage" class="progress-message"></div>
</section>
<section class="queue-panel">
 <section class="queue-head">
  <div class="queue-heading"><div class="queue-title-row"><div><span class="section-kicker">CHANNELS PIPELINE</span><h2>视频号队列</h2></div></div><p>每条视频独立保存标题、描述、封面和合集设置。</p></div>
 </section>
 <div class="queue-tools">
  <div class="media-picker">
   <button class="primary media-picker-trigger" id="mediaPickerBtn" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="mediaPickerMenu">选择视频 <span class="media-picker-caret">▾</span></button>
   <div class="media-picker-menu" id="mediaPickerMenu" role="menu" hidden>
    <button class="secondary media-picker-option" id="folderBtn" type="button" role="menuitem">▣ 选择视频文件夹</button>
    <button class="secondary media-picker-option" id="filesBtn" type="button" role="menuitem">＋ 选择单个/多个视频</button>
   </div>
  </div>
  <div class="media-picker" id="coverImportPicker">
   <button class="secondary media-picker-trigger" id="coverImportBtn" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="coverImportMenu">导入封面 <span class="media-picker-caret">▾</span></button>
   <div class="media-picker-menu" id="coverImportMenu" role="menu" hidden>
    <button class="secondary media-picker-option" id="coverFilesBtn" type="button" role="menuitem">导入封面图片（可多选）</button>
    <button class="secondary media-picker-option" id="coverFolderBtn" type="button" role="menuitem">导入封面文件夹</button>
   </div>
  </div>
  <input id="coverFileInput" type="file" accept="image/jpeg,image/png,image/webp" multiple hidden>
  </div>
  <div class="cover-import-box">
   <span id="coverImportHint" class="cover-import-hint" role="status" aria-live="polite">支持 JPG/PNG/WEBP；按歌曲名、编号和已有抖音封面文件名智能匹配 3:4 / 4:3，可分批导入。</span>
   <div id="coverImportResult" class="cover-import-result" hidden></div>
  </div>
  <div class="copy-tools">
   <div class="media-picker" id="copyMenuPicker">
    <button class="secondary accent-secondary media-picker-trigger" id="copyMenuBtn" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="copyMenu">文案 <span class="media-picker-caret">▾</span></button>
    <div class="media-picker-menu" id="copyMenu" role="menu" hidden>
     <button class="secondary media-picker-option" id="generateCopyBtn" type="button" role="menuitem">一键生成文案</button>
     <button class="secondary media-picker-option" id="importCopyBtn" type="button" role="menuitem">导入文案</button>
     <button class="secondary media-picker-option" id="downloadTemplateBtn" type="button" role="menuitem">下载文案模板</button>
    </div>
   </div>
   <input id="copyFileInput" type="file" accept=".xlsx,.xls,.json,.csv,.txt,.md" hidden>
   <label class="copy-overwrite"><input id="copyOverwriteInput" type="checkbox" checked> 替换现有文案</label>
   <span id="copyGenerateHint" class="copy-hint" role="status" aria-live="polite"></span>
  </div>
  <div id="copyGenerateResult" class="copy-result" hidden></div>
  <div class="batch-collection">
   <label class="batch-select-all"><input id="selectAllItems" type="checkbox"> 全选</label>
   <span id="selectedCount" class="batch-count">已选 0 条</span>
   <select id="batchCollectionSelect"></select>
   <button id="applyBatchCollection" class="primary" type="button">批量设置合集</button>
   <span id="batchCollectionHint" class="copy-hint"></span>
  </div>
  <span class="hint">支持选择文件夹，或直接多选视频文件；自动带出同路径抖音封面。</span>
 <p class="hint" style="padding:0 18px">封面按每条视频的 3:4 竖版、4:3 横版分别上传；短标题自动按 6–16 字规则处理；合集读取后可逐条选择。</p>
 <div id="list" class="queue"></div>
</section>
</div>
<aside class="side-column">
 <section class="notice workflow-card"><div class="card-icon">01</div><div><strong>当前动线</strong><span>选择视频 → 上传后双封面 → 短标题 → 描述 → 视频标注 → 合集 → 发表。标题超字数会自动拆分，封面失败会保留页面暂停。</span></div></section>
 <section class="side-note"><div class="side-note-title"><span class="side-note-dot"></span>安全提醒</div><p>视频号使用独立队列和独立 Chrome，不会读写抖音队列；切换平台可回到入口。读取合集使用微型测试视频，不会发表。</p></section>
</aside>
</div>
</main>
<script>
let state={items:[],publish:{},collections:{collections:[]}};
let lastSignature='';
const $=s=>document.querySelector(s);
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(url,opt){const r=await fetch(url,opt);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;}
function statusText(s){return {pending:'待发布',publishing:'发布中',published:'已提交',failed:'失败待处理'}[s]||s||'待发布';}
function collectionOptions(current){
 const names=(state.collections.collections||[]).map(x=>x.name).filter(Boolean);
 if(current&&!names.includes(current))names.unshift(current);
 return '<option value="">不加入合集</option>'+names.map(n=>`<option value="${esc(n)}" ${n===current?'selected':''}>${esc(n)}</option>`).join('');
}
const selectedChannelItems = new Set();
function updateBatchSelectionUi(){
 const total=(state.items||[]).length;const selected=selectedChannelItems.size;
 const count=$('#selectedCount');if(count)count.textContent=`已选 ${selected} 条`;
 const all=$('#selectAllItems');if(all){all.checked=total>0&&selected===total;all.indeterminate=selected>0&&selected<total;}
}
function updateBatchCollectionOptions(){
 const select=$('#batchCollectionSelect');if(!select)return;
 const previous=select.value;
 const names=(state.collections.collections||[]).map(x=>x.name).filter(Boolean);
 select.innerHTML='<option value="">不加入合集</option>'+names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
 if(previous&&names.includes(previous))select.value=previous;
}
function renderList(){
 const items=state.items||[];
 if(!items.length){$('#list').innerHTML='<div class="panel muted">暂无视频，请选择文件夹或加入单个视频。</div>';lastSignature='';return;}
 $('#list').innerHTML=items.map((item,index)=>{
  const tone=index%10;
  const id=esc(item.id||item.path);
  const portrait=item.cover_image_portrait||'';
  const landscape=item.cover_image_landscape||'';
  const portraitLabel=item.cover_image_portrait_source||item.cover_image_portrait_name||portrait;
  const landscapeLabel=item.cover_image_landscape_source||item.cover_image_landscape_name||landscape;
  const portraitName=item.cover_image_portrait_name||'';
  const landscapeName=item.cover_image_landscape_name||'';
  return `<div class="card queue-tone-${tone}${item.status==='published'?' published':''}" data-id="${id}" data-previous-status="${esc(item.status||'pending')}">
   <div class="item-head"><label class="item-select"><input type="checkbox" data-select ${selectedChannelItems.has(id)?'checked':''}></label><div><b>${esc(item.name)}</b><div class="hint">${esc(item.path)}</div></div><select class="status-select" data-status-select aria-label="视频状态"><option value="pending" ${item.status==='pending'?'selected':''}>待发布</option><option value="publishing" disabled ${item.status==='publishing'?'selected':''}>发布中</option><option value="published" ${item.status==='published'?'selected':''}>已提交</option><option value="failed" ${item.status==='failed'?'selected':''}>失败待处理</option><option value="skipped" ${item.status==='skipped'?'selected':''}>已跳过</option></select></div>
   <div class="fields">
    <label>短标题<input type="text" data-field="title" value="${esc(item.title||item.work_title||'')}" maxlength="40"></label>
    <label>话题（逗号分隔）<input type="text" data-field="hashtags" value="${esc(Array.isArray(item.hashtags)?item.hashtags.join(','):(item.hashtags||''))}"></label>
    <label class="wide">描述<textarea data-field="caption">${esc(item.caption||'')}</textarea></label>
    <label class="wide">合集<select data-field="collection_name">${collectionOptions(item.collection_name||'')}</select></label>
    <label>发布时间<select data-field="schedule_mode"><option value="immediate" ${item.schedule_mode!=='scheduled'?'selected':''}>立即发布</option><option value="scheduled" ${item.schedule_mode==='scheduled'?'selected':''}>定时发布</option></select></label>
    <label>定时时间<input type="datetime-local" data-field="schedule_at" value="${esc(String(item.schedule_at||'').slice(0,16))}"></label>
   </div>
   <div class="cover-row" style="margin-top:10px">
    <span class="cover-label">3:4 竖封面</span><span class="cover-name" title="${esc(portraitLabel||'未设置')}">${esc(portraitLabel||'未设置')}</span>
    <span class="cover-label">4:3 横封面</span><span class="cover-name" title="${esc(landscapeLabel||'未设置')}">${esc(landscapeLabel||'未设置')}</span>
   </div>
   <div class="card-actions"><button class="primary" data-save>保存本条</button></div>
  </div>`;
 }).join('');
 lastSignature=items.map(x=>String(x.id||x.path)).join('|');
 updateBatchSelectionUi();updateBatchCollectionOptions();
}
function updateStats(){
 const items=state.items||[];
 $('#statPending').textContent=items.filter(x=>x.status==='pending').length;
 $('#statPublished').textContent=items.filter(x=>x.status==='published').length;
 $('#statFailed').textContent=items.filter(x=>x.status==='failed').length;
 $('#statTotal').textContent=items.length;
 $('#statCollections').textContent=(state.collections.collections||[]).length;
}
function updatePublish(){
 const p=state.publish||{};
 $('#msg').textContent=p.message||'';
 const total=Number(p.total||0);const completed=Number(p.completed||0);
 const stage=Number(p.stage||0);const stagePercent=Number(p.stage_percent);
 const stageWeights={2:.55,3:.65,4:.75,5:.85,6:.97};
 let fraction=0;
 if(p.running&&completed<total){
  // 第一条视频发布完成前 completed 一直是 0，进度条不能只看 completed，
  // 否则上传/封面阶段看起来像卡死。按当前步骤估算当前这条的完成度。
  fraction=stage===1?(Number.isFinite(stagePercent)?0.4*Math.max(0,Math.min(100,stagePercent))/100:0):(stageWeights[stage]||0);
 }
 const pct=total?Math.max(0,Math.min(100,Math.round((completed+fraction)/total*100))):0;
 const box=$('#publishProgress');
 if(box){
  box.hidden=!(p.running||p.paused||total);
  $('#progressTitle').textContent=p.paused?'视频号已暂停':(p.running?'视频号发布中':'视频号发布');
  $('#progressCount').textContent=`${completed}/${total} 条 · ${pct}%`;
  $('#progressFill').style.width=`${pct}%`;
  const track=document.querySelector('.progress-track');
  if(track)track.classList.toggle('is-busy',Boolean(p.running)&&stage<=1&&!Number.isFinite(stagePercent));
  $('#progressMessage').textContent=p.message||'';
 }
 $('#publishBtn').disabled=Boolean(p.running);
 $('#pauseBtn').hidden=!p.running||Boolean(p.paused);
 $('#resumeBtn').hidden=!p.running||!p.paused;
 $('#stopBtn').hidden=!p.running&&!p.paused;
 $('#pauseBtn').disabled=Boolean(p.pause_requested);
}
async function refresh(force=false){
 try{
  state=await api('/api/state');
  const sig=(state.items||[]).map(x=>String(x.id||x.path)).join('|');
  if(force||sig!==lastSignature)renderList();
  document.querySelectorAll('[data-status-select]').forEach(el=>{const card=el.closest('[data-id]');const item=(state.items||[]).find(x=>String(x.id||x.path)===card.dataset.id);if(item&&el.value!==String(item.status||'pending'))el.value=String(item.status||'pending');});
  updateStats();updatePublish();updateCopyButtons();updateBatchSelectionUi();updateBatchCollectionOptions();
 }catch(e){$('#msg').textContent=e.message;}
}
async function saveItem(card){
 const id=card.dataset.id;const body={};
 card.querySelectorAll('[data-field]').forEach(el=>body[el.dataset.field]=el.value);
 try{await api('/api/items/'+encodeURIComponent(id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('#msg').textContent='已保存本条设置';await refresh(true);}catch(e){$('#msg').textContent='保存失败：'+e.message;}
}
function toggleMediaMenu(open){
 const menu=$('#mediaPickerMenu');const btn=$('#mediaPickerBtn');
 const next=open===undefined?menu.hidden:open;
 menu.hidden=!next;btn.setAttribute('aria-expanded',String(next));
}
$('#mediaPickerBtn').onclick=(ev)=>{ev.stopPropagation();toggleMediaMenu();};
document.addEventListener('click',(ev)=>{if(!ev.target.closest('.media-picker'))toggleMediaMenu(false);});
document.addEventListener('keydown',(ev)=>{if(ev.key==='Escape')toggleMediaMenu(false);});
$('#folderBtn').onclick=async()=>{toggleMediaMenu(false);$('#msg').textContent='请选择视频文件夹…';try{const r=await api('/api/select-folder',{method:'POST'});$('#msg').textContent=r.cancelled?'已取消':`已加入 ${r.count||0} 个视频（自动带出同路径抖音封面）`;await refresh(true);}catch(e){$('#msg').textContent=e.message;}};
$('#filesBtn').onclick=async()=>{toggleMediaMenu(false);$('#msg').textContent='请选择一个或多个视频文件…';try{const r=await api('/api/select-files',{method:'POST'});$('#msg').textContent=r.cancelled?'已取消':`已加入 ${r.count||0} 个视频（自动带出同路径抖音封面）`;await refresh(true);}catch(e){$('#msg').textContent=e.message;}};
function toggleCopyMenu(open){
 const menu=$('#copyMenu');const btn=$('#copyMenuBtn');
 const next=open===undefined?menu.hidden:open;
 menu.hidden=!next;btn.setAttribute('aria-expanded',String(next));
}
function confirmReplaceCopy(){
 if(!$('#copyOverwriteInput').checked)return true;
 const hasCopy=(state.items||[]).some(item=>String(item.title||item.work_title||'').trim()||String(item.caption||'').trim()||String(item.hashtags||'').trim());
 if(!hasCopy)return true;
 return confirm('将替换现有文案，是否继续？');
}
$('#copyMenuBtn').onclick=(ev)=>{ev.stopPropagation();toggleCopyMenu();};
document.addEventListener('click',(ev)=>{if(!ev.target.closest('#copyMenuPicker'))toggleCopyMenu(false);});
document.addEventListener('keydown',(ev)=>{if(ev.key==='Escape')toggleCopyMenu(false);});
$('#importCopyBtn').onclick=()=>{toggleCopyMenu(false);if(!confirmReplaceCopy())return;$('#copyFileInput').click();};
$('#copyFileInput').onchange=async()=>{const file=$('#copyFileInput').files[0];if(!file)return;const fd=new FormData();fd.append('file',file);const overwrite=$('#copyOverwriteInput').checked?'1':'0';$('#msg').textContent='正在导入文案…';try{const r=await api('/api/import-copy?overwrite='+overwrite,{method:'POST',body:fd});$('#msg').textContent=`文案导入完成：匹配 ${r.matched||0} 条${(r.skipped||[]).length?`，未覆盖 ${r.skipped.length} 条`:''}`;await refresh(true);}catch(e){$('#msg').textContent='导入文案失败：'+e.message;}finally{$('#copyFileInput').value='';}};
function updateCopyButtons(){
 const disabled=Boolean((state.publish||{}).running)||!((state.items||[]).length);
 const g=$('#generateCopyBtn');const d=$('#downloadTemplateBtn');const o=$('#copyOverwriteInput');
 if(g){g.disabled=disabled;g.textContent='一键生成文案';}
 if(d)d.disabled=disabled;
 if(o)o.disabled=disabled;
}
$('#generateCopyBtn').onclick=async()=>{
 toggleCopyMenu(false);
 const button=$('#generateCopyBtn');const hint=$('#copyGenerateHint');const box=$('#copyGenerateResult');
 if(!confirmReplaceCopy())return;
 if((state.publish||{}).running)return;
 button.disabled=true;button.textContent='生成中…';hint.classList.remove('is-error');hint.textContent='正在按歌名匹配文案库…';box.hidden=true;
 try{
  const payload=await api('/api/copy-generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({overwrite:Boolean($('#copyOverwriteInput').checked)})});
  const generated=payload.generated||[];const skipped=payload.skipped||[];const unmatched=payload.unmatched||[];
  hint.classList.toggle('is-error',!generated.length&&unmatched.length>0);
  hint.textContent=payload.message||`已生成 ${generated.length} 条文案`;
  const rows=[];
  if(generated.length)rows.push(`<strong class="ok">已生成</strong><ul>${generated.map(x=>`<li>${esc(x.name)}${x.song?` ← ${esc(x.song)}`:''}</li>`).join('')}</ul>`);
  if(skipped.length)rows.push(`<strong>保留手写</strong><ul>${skipped.map(x=>`<li>${esc(x.name)}：${esc(x.reason||'')}</li>`).join('')}</ul>`);
  if(unmatched.length)rows.push(`<strong class="bad">未匹配</strong><ul>${unmatched.map(x=>`<li>${esc(x.name)}：${esc(x.reason||'')}</li>`).join('')}</ul>`);
  box.hidden=!rows.length;box.innerHTML=rows.join('');await refresh(true);
 }catch(e){hint.textContent='生成文案失败：'+e.message;hint.classList.add('is-error');box.hidden=false;box.innerHTML=`<strong class="bad">生成失败</strong><div>${esc(e.message)}</div>`;}
 finally{button.disabled=false;button.textContent='一键生成文案';updateCopyButtons();}
};
$('#downloadTemplateBtn').onclick=async()=>{
 const button=$('#downloadTemplateBtn');button.disabled=true;button.textContent='生成模板中…';
 try{
  const response=await fetch('/api/caption-template',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(!response.ok){const d=await response.json().catch(()=>({}));throw new Error(d.error||`HTTP ${response.status}`);}
  const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='视频号文案模板.xlsx';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
  $('#copyGenerateHint').textContent='已下载视频号文案模板，填好后可用“导入文案”回填';
 }catch(e){$('#copyGenerateHint').textContent='下载模板失败：'+e.message;$('#copyGenerateHint').classList.add('is-error');}
 finally{button.disabled=false;button.textContent='下载文案模板';updateCopyButtons();}
};
function importCoverFiles(files){
 const button=$('#coverImportBtn');const hint=$('#coverImportHint');const resultBox=$('#coverImportResult');
 if(!files.length)return;
 if((state.publish||{}).running){hint.textContent='视频号发布任务运行中，暂不能导入封面';hint.classList.add('is-error');return;}
 button.disabled=true;button.textContent='匹配中…';hint.classList.remove('is-error');hint.textContent=`正在上传 ${files.length} 张封面…`;resultBox.hidden=true;resultBox.innerHTML='';
 const fd=new FormData();files.forEach(f=>fd.append('files',f,f.name));
 const xhr=new XMLHttpRequest();xhr.open('POST','/api/covers/import');
 xhr.upload.onprogress=(ev)=>{if(ev.lengthComputable){const pct=Math.round(ev.loaded/ev.total*100);hint.textContent=`正在上传 ${files.length} 张封面：${pct}%`;}};
 xhr.onload=()=>{
  button.disabled=false;button.textContent='导入封面';
  let data={};try{data=JSON.parse(xhr.responseText||'{}');}catch(e){}
  if(xhr.status<200||xhr.status>=300||data.error){
   hint.textContent='封面导入失败：'+(data.error||`HTTP ${xhr.status}`);
   hint.classList.add('is-error');resultBox.hidden=false;resultBox.innerHTML=`<strong class="bad">导入失败</strong><div>${esc(data.error||('HTTP '+xhr.status))}</div>`;
   return;
  }
  const matched=Array.isArray(data.matched)?data.matched:[];const unmatched=Array.isArray(data.unmatched)?data.unmatched:[];
  hint.classList.toggle('is-error',matched.length===0);
  hint.textContent=`本轮导入完成：匹配 ${matched.length} 张${unmatched.length?`，未匹配 ${unmatched.length} 张`:''}${data.total?`（选择 ${data.total} 张）`:''}`;
  const rows=[];
  if(matched.length)rows.push(`<strong class="ok">已匹配并写回队列</strong><ul>${matched.map(m=>`<li>${esc(m.video)} ← ${esc(m.cover)}（${esc(m.orientation||'')}，${m.action==='replaced'?'替换':'新增'}）</li>`).join('')}</ul>`);
  if(unmatched.length)rows.push(`<strong class="bad">未匹配</strong><ul>${unmatched.map(m=>`<li>${esc(m.cover)}：${esc(m.reason||'')}</li>`).join('')}</ul>`);
  resultBox.hidden=!rows.length;resultBox.innerHTML=rows.join('');
  refresh(true).catch(()=>{});
 };
 xhr.onerror=()=>{button.disabled=false;button.textContent='导入封面';hint.textContent='封面导入失败：网络请求失败，请检查本地服务';hint.classList.add('is-error');};
 xhr.onloadend=()=>{button.disabled=false;button.textContent='导入封面';$('#coverFileInput').value='';};
 xhr.send(fd);
}
function toggleCoverMenu(open){
 const menu=$('#coverImportMenu');const btn=$('#coverImportBtn');
 const next=open===undefined?menu.hidden:open;
 menu.hidden=!next;btn.setAttribute('aria-expanded',String(next));
}
$('#coverImportBtn').onclick=(ev)=>{ev.stopPropagation();toggleCoverMenu();};
$('#coverFilesBtn').onclick=()=>{toggleCoverMenu(false);$('#coverFileInput').click();};
document.addEventListener('click',(ev)=>{if(!ev.target.closest('#coverImportPicker'))toggleCoverMenu(false);});
document.addEventListener('keydown',(ev)=>{if(ev.key==='Escape')toggleCoverMenu(false);});
$('#coverFileInput').onchange=()=>{importCoverFiles([...($('#coverFileInput').files||[])]);};
$('#coverFolderBtn').onclick=async()=>{
 toggleCoverMenu(false);
 const button=$('#coverFolderBtn');const hint=$('#coverImportHint');const resultBox=$('#coverImportResult');
 if((state.publish||{}).running){hint.textContent='视频号发布任务运行中，暂不能导入封面';hint.classList.add('is-error');return;}
 button.disabled=true;button.textContent='匹配中…';hint.classList.remove('is-error');hint.textContent='请选择封面文件夹…';resultBox.hidden=true;
 try{
  const data=await api('/api/covers/smart-import',{method:'POST'});
  if(data.cancelled){hint.textContent='已取消选择文件夹';return;}
  if(data.error){throw new Error(data.error);}
  const matched=Array.isArray(data.matched)?data.matched:[];const unmatched=Array.isArray(data.unmatched)?data.unmatched:[];
  hint.classList.toggle('is-error',matched.length===0);
  hint.textContent=`本轮文件夹导入完成：匹配 ${matched.length} 张${unmatched.length?`，未匹配 ${unmatched.length} 张`:''}`;
  const rows=[];
  if(matched.length)rows.push(`<strong class="ok">已匹配并写回队列</strong><ul>${matched.map(m=>`<li>${esc(m.video)} ← ${esc(m.cover)}（${esc(m.orientation||'')}，${m.action==='replaced'?'替换':'新增'}）</li>`).join('')}</ul>`);
  if(unmatched.length)rows.push(`<strong class="bad">未匹配</strong><ul>${unmatched.map(m=>`<li>${esc(m.cover)}：${esc(m.reason||'')}</li>`).join('')}</ul>`);
  resultBox.hidden=!rows.length;resultBox.innerHTML=rows.join('');
  await refresh(true);
 }catch(e){hint.textContent='封面文件夹导入失败：'+e.message;hint.classList.add('is-error');resultBox.hidden=false;resultBox.innerHTML=`<strong class="bad">导入失败</strong><div>${esc(e.message)}</div>`;}
 finally{button.disabled=false;button.textContent='导入封面文件夹';}
};
$('#checkBtn').onclick=async()=>{if($('#checkBtn').disabled)return;$('#checkBtn').disabled=true;$('#msg').textContent='正在检查视频号发布环境…';try{const r=await api('/api/check-env',{method:'POST'});$('#msg').textContent=r.message||(r.ok?'环境正常':'环境异常');}catch(e){$('#msg').textContent='检查失败：'+e.message;}finally{$('#checkBtn').disabled=false;}};
$('#collectionsBtn').onclick=async()=>{if(!$('#collectionsBtn').disabled){$('#collectionsBtn').disabled=true;$('#msg').textContent='正在读取视频号合集…';try{const r=await api('/api/collections/refresh',{method:'POST'});state.collections=r;$('#msg').textContent=`已读取 ${(r.collections||[]).length} 个合集`;renderList();}catch(e){$('#msg').textContent='读取合集失败：'+e.message;}finally{$('#collectionsBtn').disabled=false;}}};
$('#publishBtn').onclick=async()=>{try{await api('/api/publish',{method:'POST'});$('#msg').textContent='已启动视频号批量发布';await refresh();}catch(e){$('#msg').textContent=e.message;}};
$('#pauseBtn').onclick=async()=>{try{await api('/api/pause',{method:'POST'});await refresh();}catch(e){$('#msg').textContent=e.message;}};
$('#resumeBtn').onclick=async()=>{try{await api('/api/resume',{method:'POST'});await refresh();}catch(e){$('#msg').textContent=e.message;}};
$('#stopBtn').onclick=async()=>{if(!confirm('终止视频号发布？当前视频会标记为失败，浏览器关闭。'))return;try{await api('/api/terminate',{method:'POST'});await refresh();}catch(e){$('#msg').textContent=e.message;}};
$('#clearBtn').onclick=async()=>{if(!confirm('清空视频号独立队列？不会删除源视频。'))return;await api('/api/items/clear',{method:'POST'});await refresh(true);};
$('#selectAllItems').onchange=()=>{
 const checked=$('#selectAllItems').checked;
 selectedChannelItems.clear();
 if(checked)(state.items||[]).forEach(x=>selectedChannelItems.add(String(x.id||x.path)));
 renderList();
};
$('#list').addEventListener('change',async ev=>{
 const statusSelect=ev.target.closest('[data-status-select]');
 if(statusSelect){
  const card=statusSelect.closest('.card');const id=card?card.dataset.id:'';if(!id)return;
  const previous=card.dataset.previousStatus||'pending';
  if(statusSelect.value==='pending'&&previous==='published'&&!confirm('改为待发布后，这条视频会重新进入发布队列，可能产生重复作品。是否继续？')){statusSelect.value=previous;return;}
  statusSelect.disabled=true;
  try{const r=await api('/api/items/'+encodeURIComponent(id)+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:statusSelect.value})});card.dataset.previousStatus=r.status;if(r.status==='pending'){$('#msg').textContent='已改为待发布，下次开始发布会重新处理这条视频';}else{$('#msg').textContent='视频状态已更新为：'+statusSelect.options[statusSelect.selectedIndex].text;}await refresh(true);}catch(e){statusSelect.value=previous;$('#msg').textContent='修改状态失败：'+e.message;}finally{statusSelect.disabled=false;}
  return;
 }
 const box=ev.target.closest('[data-select]');if(!box)return;
 const card=box.closest('.card');const id=card?card.dataset.id:'';
 if(!id)return;
 if(box.checked)selectedChannelItems.add(id);else selectedChannelItems.delete(id);
 updateBatchSelectionUi();
});
$('#applyBatchCollection').onclick=async()=>{
 const ids=[...selectedChannelItems];
 const hint=$('#batchCollectionHint');
 if(!ids.length){hint.textContent='请先勾选要设置合集的视频';hint.classList.add('is-error');return;}
 const name=$('#batchCollectionSelect').value;
 hint.classList.remove('is-error');hint.textContent='正在批量设置合集…';
 try{
  const r=await fetch('/api/items/bulk-collection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,collection_name:name})}).then(async res=>{const d=await res.json().catch(()=>({}));if(!res.ok)throw new Error(d.error||`HTTP ${res.status}`);return d;});
  hint.textContent=`已为 ${r.count||0} 条视频设置合集：${name||'不加入合集'}`;
  selectedChannelItems.clear();await refresh(true);
 }catch(e){hint.textContent='批量设置合集失败：'+e.message;hint.classList.add('is-error');}
};
$('#list').addEventListener('click',async ev=>{
 const card=ev.target.closest('.card');if(!card)return;
 if(ev.target.matches('[data-save]')){saveItem(card);return;}
 const cover=ev.target.closest('[data-cover]');
 if(cover){const kind=cover.dataset.cover;try{$('#msg').textContent='请选择封面文件…';await api('/api/items/'+encodeURIComponent(card.dataset.id)+'/cover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind})});await refresh(true);}catch(e){$('#msg').textContent='封面上传失败：'+e.message;}return;}
});
refresh();setInterval(()=>refresh(false),2000);
</script>'''

if __name__=='__main__': print(f'视频号工具: http://127.0.0.1:{PORT}',flush=True); ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
