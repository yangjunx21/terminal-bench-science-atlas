const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM}=require('jsdom');

function reader(){
  const dom=new JSDOM('<!doctype html><div id="out"></div>',{runScripts:'outside-only',url:'http://localhost/'});
  for(const file of ['vendor/markdown-it/markdown-it.min.js','vendor/katex/katex.min.js','vendor/texmath/texmath.js','vendor/dompurify/purify.min.js','vendor/highlight/highlight.min.js','render.js']){
    new vm.Script(fs.readFileSync(path.join(__dirname,'web',file),'utf8'),{filename:file}).runInContext(dom.getInternalVMContext());
  }
  return dom;
}

test('headings, tables, fenced code and four TeX delimiter forms render together',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  const source='# Scientific result\n\n**Strong** and $e^{i\\pi}+1=0$, \\(x^2\\).\n\n$$\\frac{a}{b}$$\n\n\\[\\sum_{i=1}^{n} i\\]\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1 < 2\nprint(x)\n```';
  out.innerHTML=dom.window.ScienceMarkdown.html(source);dom.window.ScienceMarkdown.bind(out);
  assert.equal(out.querySelector('h1').textContent,'Scientific result');
  assert.ok(out.querySelector('strong'));
  assert.equal(out.querySelectorAll('.katex').length,4);
  assert.ok(out.querySelector('table'));
  assert.equal(out.querySelector('pre code').textContent,'x = 1 < 2\nprint(x)\n');
  assert.ok(out.querySelector('code .hljs-number'));
  assert.equal(out.querySelectorAll('.math-copy').length,2);
  dom.window.close();
});

test('source markup, hostile URLs and trusted TeX commands cannot create executable content',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  out.innerHTML=dom.window.ScienceMarkdown.html('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))\n\n$\\href{javascript:alert(1)}{x}$\n\n$\\htmlStyle{background:url(javascript:alert(1))}{x}$');
  assert.equal(out.querySelectorAll('script,iframe,img,style,form,object').length,0);
  for(const element of out.querySelectorAll('*')){
    for(const attr of element.attributes){
      assert.ok(!/^on/i.test(attr.name));
      if(attr.name==='href') assert.ok(!/^javascript:/i.test(attr.value));
    }
  }
  dom.window.close();
});

test('code, currency, file paths and malformed equations retain readable source',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  out.innerHTML=dom.window.ScienceMarkdown.html('Costs $12 and $30. `x = $not_math$`\n\n[solver](/app/solver.py) [source](https://example.com)\n\n$$\\frac{broken$$');
  assert.equal(out.querySelectorAll('a').length,1);
  assert.equal(out.querySelector('a').getAttribute('rel'),'noopener noreferrer');
  assert.equal(out.querySelector('.source-file').textContent,'solver');
  assert.equal(out.querySelector('code').textContent,'x = $not_math$');
  assert.ok(out.textContent.includes('Costs $12 and $30.'));
  assert.ok(out.querySelector('.katex-error'));
  out.innerHTML=dom.window.ScienceMarkdown.html('普通金额 $12 and $30；代码 `x = "$a$"` 保持原样。');
  assert.equal(out.querySelectorAll('.katex').length,0);
  assert.equal(out.querySelector('code').textContent,'x = "$a$"');
  assert.ok(out.textContent.includes('$12 and $30；'));
  dom.window.close();
});

test('macros do not leak from one model message into another',()=>{
  const dom=reader(),render=dom.window.ScienceMarkdown.html;
  render('$\\gdef\\modelonly{SECRET}\\modelonly$');
  const next=render('$\\modelonly$');
  assert.ok(!next.includes('SECRET'));
  assert.ok(!next.includes('message-source'));
  dom.window.close();
});
