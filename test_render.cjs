const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM}=require('jsdom');

function reader(){
  const dom=new JSDOM('<!doctype html><div id="out"></div>',{runScripts:'outside-only',url:'http://localhost/'});
  for(const file of ['vendor/markdown-it/markdown-it.min.js','vendor/katex/katex.min.js','vendor/texmath/texmath.js','vendor/dompurify/purify.min.js','vendor/highlight/highlight.min.js','render.js','tools.js']){
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

test('real Codex repr envelopes expose multiline outputs without changing the source',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  const step=JSON.parse(fs.readFileSync(path.join(__dirname,'tests/fixtures/codex-tool-step.json'),'utf8'));
  const original=JSON.stringify(step);
  dom.window.ScienceTools.appendStep(out,step);
  assert.equal(out.querySelectorAll('details').length,0);
  assert.equal(out.querySelectorAll('.tool-call').length,1);
  assert.equal(out.querySelectorAll('.tool-response').length,1);
  const outputs=[...out.querySelectorAll('.tool-response pre code')].map(code=>code.textContent);
  assert.ok(outputs.includes('Script completed\nWall time 0.1 seconds\nOutput:\n'));
  assert.ok(outputs.includes('/app\ntraffic_solver.py\nflux_models.py\ndata/episodes.npz\n'));
  assert.ok(outputs.some(text=>text.startsWith('total 3\ndrwx')));
  assert.equal(out.querySelector('.tool-call pre code').textContent,step.tool_calls[0].arguments.input);
  assert.equal(JSON.stringify(step),original);
  dom.window.close();
});

test('parallel calls pair with their own responses and code arguments preserve indentation and escapes',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  const code='def main():\n    pattern = r"\\n"\n    print("<img onerror=alert(1)>")\n';
  dom.window.ScienceTools.appendStep(out,{
    tool_calls:[
      {tool_call_id:'write',function_name:'Write',arguments:JSON.stringify({file_path:'/app/solve.py',content:code})},
      {id:'run',function:{name:'Bash',arguments:JSON.stringify({command:'python solve.py\nprintf "done\\n"'})}},
    ],
    observation:{results:[{source_call_id:'run',content:{stdout:'answer\n42\n',stderr:'warning\n'}},{source_call_id:'write',content:'saved'}]},
  });
  assert.deepEqual([...out.children].map(block=>block.querySelector('.tool-heading').textContent),['Tool callWrite','Tool responseWrite','Tool callBash','Tool responseBash']);
  assert.equal(out.querySelector('.tool-call code.hljs').textContent,code);
  assert.ok(out.querySelector('.hljs-keyword'));
  assert.equal(out.querySelectorAll('img,script').length,0);
  assert.ok([...out.querySelectorAll('.tool-response code')].some(block=>block.textContent==='answer\n42\n'));
  const next=dom.window.document.createElement('div');
  dom.window.ScienceTools.appendStep(next,{source:'tool',tool_call_id:'run',message:'C:\\new\\test; literal \\n'},new Map([['run','Bash']]));
  assert.equal(next.querySelector('code').textContent,'C:\\new\\test; literal \\n');
  assert.equal(next.querySelector('.tool-heading').textContent,'Tool responseBash');
  dom.window.close();
});

test('response literals handle nested JSON, unknown fields and malformed or executable-looking text safely',()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  const source="__import__('os').system('unsafe')";
  dom.window.ScienceTools.appendStep(out,{observation:{results:[
    {content:JSON.stringify({content:[{type:'text',text:JSON.stringify({stdout:'a\nb',exit_code:0})}]})},
    {content:{answer:42,valid:true}},
    {content:source},
    {content:"[{'type': 'text', 'text': '<script>unsafe()</script>'}]"},
    {content:"[{'text': 'unterminated'}"},
  ]}});
  const outputs=[...out.querySelectorAll('code')].map(code=>code.textContent);
  assert.ok(outputs.includes('a\nb'));
  assert.ok(outputs.some(text=>text.includes('"answer": 42') && text.includes('\n')));
  assert.ok(outputs.includes(source));
  assert.ok(outputs.includes('<script>unsafe()</script>'));
  assert.ok(outputs.includes("[{'text': 'unterminated'}"));
  assert.equal(out.querySelectorAll('script').length,0);
  assert.equal(dom.window.ScienceTools.decode("{'a': True, 'b': False, 'c': None}").c,null);
  assert.equal(dom.window.ScienceTools.decode("{'__proto__': {'polluted': True}}").__proto__.polluted,true);
  assert.equal({}.polluted,undefined);
  dom.window.close();
});

test('exceptionally large output can be copied in full before its remaining text is shown',async()=>{
  const dom=reader(),out=dom.window.document.querySelector('#out');
  const source='start\n'+'x'.repeat(65000)+'\nend';let copied;
  Object.defineProperty(dom.window.navigator,'clipboard',{value:{writeText:async text=>{copied=text;}}});
  dom.window.ScienceTools.appendStep(out,{observation:{results:[{content:source}]}});
  assert.equal(out.querySelector('code').textContent.length,60000);
  await out.querySelector('.code-copy').onclick();assert.equal(copied,source);
  out.querySelector('.button').click();assert.equal(out.querySelector('code').textContent,source);
  dom.window.close();
});
