import requests, trafilatura
from bs4 import BeautifulSoup

def fetch_url_text(url:str,timeout:int=10):
    try:
        r=requests.get(url,timeout=timeout,headers={'User-Agent':'Mozilla/5.0'})
        r.raise_for_status(); html=r.text
        text=trafilatura.extract(html) or BeautifulSoup(html,'html.parser').get_text(' ',strip=True)
        title=BeautifulSoup(html,'html.parser').title
        return {'ok':True,'url':url,'title':title.text if title else None,'text':text}
    except Exception as e:
        return {'ok':False,'url':url,'error':str(e)}
