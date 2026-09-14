"""Real HA cards/editor and registry overview; runtime evidence stays in /ui."""
import json
from pathlib import Path
import sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from picture_assertions import assert_container_pictures

sys.path.insert(0, '/helpers')
from regeln import messe_text, bewerte

ROOT = Path('/ui')
URL = 'http://127.0.0.1:18123'
# State badges use CSS pictures; tiles use IMG. Probe decoding for both.
PICTURES = '''async root => {
 const result=[]; const pending=[];
 function walk(r){for(const e of r.querySelectorAll('*')){
  const background=getComputedStyle(e).backgroundImage;
  let src=e.tagName==='IMG'?e.src:background.startsWith('url(')?background.slice(5,-2):null;
  if(src && (src.includes('/local/unraid_ssh/')||src.includes('images.example.invalid'))){
   const row={src,entity:e.stateObj?.entity_id||null,kind:e.tagName,width:0,visible:e.getBoundingClientRect().width>0};result.push(row);
   if(e.tagName==='IMG')row.width=e.naturalWidth;
   else pending.push(new Promise(resolve=>{const probe=new Image();probe.onload=()=>{row.width=probe.naturalWidth;resolve()};probe.onerror=()=>resolve();probe.src=src;}));
  }
  if(e.shadowRoot)walk(e.shadowRoot);
 }}walk(root.shadowRoot || root);await Promise.all(pending);return result;
}'''
TEXT = '''root => {let text='';function walk(r){for(const e of r.childNodes){if(e.nodeType===3)text+=e.textContent+' ';else{walk(e);if(e.shadowRoot)walk(e.shadowRoot)}}}walk(root.shadowRoot || root);return text;}'''


def set_theme(page, dark):
    page.evaluate("dark => document.querySelector('home-assistant').dispatchEvent(new CustomEvent('settheme', {detail:{theme:'default',dark},bubbles:true,composed:true}))", dark)
    page.wait_for_function("dark => document.querySelector('home-assistant').hass.themes.darkMode === dark", arg=dark)
    page.wait_for_timeout(300)


def expand_busch(page):
    card = page.locator('busch-device-card')
    if not card.evaluate('e => e._offen'):
        card.locator('.dev-kopf').click()
    for button in card.locator('.dev-zu .dev-gruppe-kopf').all():
        button.click()
    page.wait_for_timeout(300)


def tag_cards(page):
    for index, card in enumerate(page.locator('hui-entities-card, hui-tile-card, busch-device-card').all()):
        card.evaluate('(e,id)=>e.id=id', f'acceptance-card-{index}')


def card_evidence(page, entities):
    results = []
    for card in page.locator('hui-entities-card, hui-tile-card, busch-device-card').all():
        ident = card.get_attribute('id')
        tag = card.evaluate('e=>e.tagName')
        config = card.evaluate('e=>e._config')
        pictures = card.evaluate(PICTURES)
        text = card.evaluate(TEXT)
        result = {'id':ident, 'tag':tag, 'config':config, 'pictures':pictures, 'text':text,
                  'measurement':messe_text(page, '#'+ident)}
        if tag == 'HUI-ENTITIES-CARD' and config.get('title') == 'Container pictures':
            assert_container_pictures(pictures, entities)
            assert 'alpha_one' in text and 'beta' in text
        if tag == 'HUI-TILE-CARD':
            assert 'alpha_one' in text
            if config.get('show_entity_picture'):
                assert len(pictures) == 1 and pictures[0]['width'] > 0
            else:
                # Existing Busch embedded tile does not expose/pass this option.
                assert not pictures
        if tag == 'BUSCH-DEVICE-CARD':
            assert 'alpha_one' in text and 'beta' in text
            assert any(i['entity'] == entities['_update_alpha_one'] and i['width'] > 0 for i in pictures)
        if tag == 'HUI-ENTITIES-CARD' and config.get('title') == 'GPU / Shares':
            assert 'GPU 0' in text and 'Media Backup' in text and 'Media_Backup' in text
        results.append(result)
    return results


def editor_evidence(page, entity, language):
    # Mount the official Tile editor obtained from HA's actual loaded card.
    # All fields and config-changed events are Home Assistant's own implementation.
    page.evaluate('''async entity => {
      const helpers=await window.loadCardHelpers();
      const tile=helpers.createCardElement({type:'tile',entity});
      const editor=await tile.constructor.getConfigElement();
      editor.hass=document.querySelector('home-assistant').hass;
      editor.setConfig({type:'tile',entity});
      editor.id='acceptance-editor';
      editor.style.cssText='position:fixed;inset:64px 12px 12px;z-index:10000;overflow:auto;background:var(--card-background-color);padding:16px';
      editor.addEventListener('config-changed',event=>{window.acceptanceEditorConfig=event.detail.config;editor.setConfig(event.detail.config)});
      document.querySelector('home-assistant').shadowRoot.append(editor);
    }''', entity)
    editor = page.locator('#acceptance-editor')
    editor.wait_for()
    page.wait_for_timeout(400)
    (ROOT/f'editor-dom-{language}.json').write_text(json.dumps({'text':editor.evaluate(TEXT),'switches':editor.locator('ha-switch').evaluate_all('es=>es.map(e=>({label:e.getAttribute("aria-label"),name:e.name,outer:e.outerHTML}))')},indent=2))
    page.screenshot(path=str(ROOT/f'editor-before-{language}.png'))
    label = 'Bild der Entität anzeigen' if language == 'de' else 'Show entity picture'
    editor.get_by_text('Inhalt' if language == 'de' else 'Content', exact=True).click()
    page.wait_for_timeout(500)
    editor.locator('ha-switch[aria-label="'+label+'"]').click()
    page.wait_for_function('window.acceptanceEditorConfig?.show_entity_picture === true')
    page.wait_for_timeout(500)
    page.screenshot(path=str(ROOT/f'tile-editor-{language}.png'))
    (ROOT/f'editor-fields-{language}.json').write_text(json.dumps(editor.locator('input, ha-textfield, ha-select, ha-selector').evaluate_all('es=>es.map(e=>({tag:e.tagName,label:e.label,placeholder:e.placeholder,type:e.type,aria:e.getAttribute("aria-label")}))'),indent=2))
    editor.locator('ha-selector').evaluate_all('es=>es.filter(e=>e.label==="Name").forEach(e=>e.id="acceptance-name")')
    name_field = editor.locator('#acceptance-name')
    name_field.get_by_text('Benutzerdefiniert' if language == 'de' else 'Custom', exact=True).click()
    name_field.locator('input').fill('alpha_one')
    name_field.locator('input').press('Tab')
    page.wait_for_function('window.acceptanceEditorConfig?.name === "alpha_one"')
    page.screenshot(path=str(ROOT/f'tile-editor-short-name-{language}.png'))
    config = page.evaluate('window.acceptanceEditorConfig')
    editor.evaluate('e=>e.remove()')
    return config


def main():
    inventory = json.loads((ROOT/'inventory.json').read_text())
    language, entities = inventory['language'], inventory['entities']
    auth = json.loads((ROOT/'auth.json').read_text())
    results, overview = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={'width':960,'height':1700}, locale=language)
        context.add_init_script('localStorage.setItem("hassTokens", '+json.dumps(json.dumps(auth))+');localStorage.setItem("selectedLanguage", '+json.dumps(language)+');')
        fallback_online, blocked = [True], []
        def route(request):
            host = urlparse(request.request.url).hostname
            if host == '127.0.0.1': request.continue_()
            elif host == 'images.example.invalid' and fallback_online[0]:
                request.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9" fill="orange"/></svg>')
            else:
                blocked.append(host)
                request.abort()
        context.route('**/*', route)
        page = context.new_page()
        page.goto(URL+'/lovelace/acceptance',wait_until='networkidle')
        confirm=page.get_by_role('button',name='Bestätigen' if language=='de' else 'Confirm',exact=True)
        if confirm.count(): confirm.click()
        page.locator('busch-device-card').wait_for()
        expand_busch(page)
        tag_cards(page)
        for width in (320,480,960):
            page.set_viewport_size({'width':width,'height':1700})
            for dark in (False,True):
                set_theme(page,dark)
                cards=card_evidence(page,entities)
                results.append({'language':language,'width':width,'dark':dark,
                    'background':page.evaluate("getComputedStyle(document.querySelector('home-assistant')).getPropertyValue('--primary-background-color')"),'cards':cards})
                page.screenshot(path=str(ROOT/f'dashboard-{language}-{width}-{"dark" if dark else "light"}.png'),full_page=True)
        editor = editor_evidence(page,entities['_container_alpha_one'],language)
        # Apply the tested native card-name setting; entity registry stays untouched.
        page.locator('hui-tile-card').evaluate_all('es=>es.filter(e=>e._config.show_entity_picture).forEach(e=>e.setConfig({...e._config,name:e._config.entity.startsWith("update.")?"alpha_one Update":"alpha_one"}))')
        entity_card = page.locator('hui-entities-card').first
        entity_config = entity_card.evaluate('e=>e._config')
        entity_config['entities'] = [{'entity':row} if isinstance(row,str) else row for row in entity_config['entities']]
        for row in entity_config['entities']:
            row['name'] = ('alpha_one' if 'alpha_one' in row['entity'] else 'beta') + (' Update' if row['entity'].startswith('update.') else '')
        entity_card.evaluate('(e,config)=>e.setConfig(config)', entity_config)
        sensor_card=page.locator('hui-entities-card').nth(1)
        sensor_config=sensor_card.evaluate('e=>e._config')
        names=page.evaluate('Object.fromEntries(Object.entries(document.querySelector("home-assistant").hass.states).map(([id,s])=>[id,s.attributes.friendly_name]))')
        sensor_config['entities']=[{'entity':row,'name':names[row].removeprefix('Example Unraid ').replace('Share ', '').replace('Example GPU ', '')} for row in sensor_config['entities']]
        sensor_card.evaluate('(e,config)=>e.setConfig(config)',sensor_config)
        page.set_viewport_size({'width':320,'height':1700})
        page.wait_for_timeout(400)
        page.screenshot(path=str(ROOT/f'short-names-{language}-320.png'),full_page=True)
        short_names=card_evidence(page,entities)
        fallback_online[0]=False
        page.reload(wait_until='networkidle')
        page.locator('busch-device-card').wait_for()
        expand_busch(page)
        pictures=page.locator('hui-view').evaluate(PICTURES)
        local=[i for i in pictures if '/local/unraid_ssh/' in i['src']]
        remote=[i for i in pictures if 'images.example.invalid' in i['src']]
        assert local and all(i['width']>0 for i in local)
        assert remote and all(i['width']==0 for i in remote)
        page.screenshot(path=str(ROOT/f'offline-{language}.png'),full_page=True)
        fallback_online[0]=True
        page.goto(URL+'/config/devices/dashboard',wait_until='networkidle')
        page.wait_for_timeout(500)
        for width in (320,480,960):
            page.set_viewport_size({'width':width,'height':1700})
            for dark in (False,True):
                set_theme(page,dark)
                body=page.locator('home-assistant').evaluate(TEXT)
                for device in inventory['devices']:
                    assert device['name'] in body, device['name']
                assert 'Example Unraid GPU' not in body
                page.screenshot(path=str(ROOT/f'devices-{language}-{width}-{"dark" if dark else "light"}.png'),full_page=True)
                overview.append({'width':width,'dark':dark,'names':[d['name'] for d in inventory['devices']],
                    'page_overflow':page.evaluate('document.documentElement.scrollWidth > innerWidth')})
        grouped=next(d for d in inventory['devices'] if ('Freie Container' in d['name'] or 'Standalone containers' in d['name']))
        page.goto(URL+'/config/devices/device/'+grouped['id'],wait_until='networkidle')
        page.set_viewport_size({'width':320,'height':1700})
        page.wait_for_timeout(600)
        detail_text=page.locator('home-assistant').evaluate(TEXT)
        assert 'alpha_one' in detail_text and 'beta' in detail_text
        page.screenshot(path=str(ROOT/f'device-detail-{language}-320.png'),full_page=True)
        for device in inventory['devices']:
            if 'Share ' not in device['name']:
                continue
            page.goto(URL+'/config/devices/device/'+device['id'],wait_until='networkidle')
            page.wait_for_timeout(400)
            text=page.locator('home-assistant').evaluate(TEXT)
            assert device['name'] in text
            assert ('Belegt' in text and 'Frei' in text) if language=='de' else ('Used' in text and 'Free' in text)
            page.screenshot(path=str(ROOT/f'share-detail-{language}-{device["name"].split("Share ")[-1]}-320.png'),full_page=True)
        report={'matrix':results,'overview':overview,'editor':editor,'short_names':short_names,'offline':{'local_loaded':len(local),'remote_failed':len(remote)},'blocked_origins':sorted(set(blocked))}
        (ROOT/'browser-report.json').write_text(json.dumps(report,indent=2))
        status=bewerte([results,short_names])
        print(json.dumps({'cases':len(results),'overview_cases':len(overview),'measurement_status':status,'offline_local_loaded':len(local),'offline_remote_failed':len(remote)}))
        browser.close()
        if status or any(o['page_overflow'] for o in overview):
            raise SystemExit(1)

if __name__=='__main__': main()
