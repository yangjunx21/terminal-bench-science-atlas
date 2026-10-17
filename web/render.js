/* Offline Markdown + TeX reader. Source trajectories are never modified. */
(function(root){
  'use strict';
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const mathOptions={trust:false,throwOnError:false,strict:'ignore',maxExpand:1000,maxSize:12,output:'htmlAndMathml'};
  function renderMath(source,options){
    return root.katex.renderToString(source,options).replace('<span class=',`<span data-latex="${escape(source)}" class=`);
  }
  let parser;
  function markdown(){
    if(parser) return parser;
    parser=root.markdownit({html:false,linkify:true,breaks:false,typographer:false})
      .use(root.texmath,{engine:{renderToString:renderMath},delimiters:['dollars','brackets','beg_end'],katexOptions:mathOptions});
    parser.inline.ruler.before('math_inline_double','currency_literal',(state,silent)=>{
      const rest=state.src.slice(state.pos);
      if(!/^\$\d[\d,]*(?:\.\d+)?(?=\s|[.,;!?，。；！？、）】]|$)/.test(rest)) return false;
      const close=rest.indexOf('$',1),inside=rest.slice(1,close<0?undefined:close);
      const currency=close<0 || /\d/.test(rest[close+1]||'') || /[`\n]/.test(inside) || (!/[\\^_=+*<>]/.test(inside) && /[a-zA-Z]{2,}/.test(inside));
      if(!currency) return false;
      if(!silent) state.push('text','',0).content='$';
      state.pos++;return true;
    });
    parser.renderer.rules.fence=(tokens,index)=>{
      const token=tokens[index],language=token.info.trim().split(/\s+/)[0].toLowerCase();
      if(['math','latex','tex'].includes(language)){
        return `<div class="math-fence">${renderMath(token.content,{...mathOptions,displayMode:true,macros:{}})}</div>`;
      }
      let code=escape(token.content);
      if(token.content.length<=60000 && language && root.hljs?.getLanguage(language)){
        try{code=root.hljs.highlight(token.content,{language,ignoreIllegals:true}).value;}catch{/* Keep literal code. */}
      }
      return `<div class="code-frame"><div class="code-toolbar"><span>${escape(language||'code')}</span><button class="code-copy" type="button">复制代码</button></div><pre><code class="hljs">${code}</code></pre></div>`;
    };
    parser.renderer.rules.image=(tokens,index)=>`<span class="image-reference">[图片引用：${escape(tokens[index].content||'see original trajectory')}]</span>`;
    parser.core.ruler.push('source_links',state=>{
      for(const block of state.tokens){
        const stack=[];
        for(const token of block.children||[]){
          if(token.type==='link_open'){
            const href=token.attrGet('href')||'';
            const safe=/^(https?:\/\/|mailto:|#)/i.test(href);
            token.tag=safe?'a':'span';stack.push(token.tag);
            if(safe){token.attrSet('target','_blank');token.attrSet('rel','noopener noreferrer');}
            else{token.attrs=[['class','source-file'],['title','源轨迹中的文件路径：'+href]];}
          }else if(token.type==='link_close') token.tag=stack.pop()||'span';
        }
      }
    });
    return parser;
  }
  function html(source){
    if(!root.markdownit || !root.texmath || !root.katex || !root.DOMPurify) return `<pre class="message-source">${escape(source)}</pre>`;
    try{
      const rendered=markdown().render(String(source),{});
      return root.DOMPurify.sanitize(rendered,{
        USE_PROFILES:{html:true,mathMl:true,svg:true},ADD_TAGS:['eq','eqn','equation','eqno'],
        FORBID_TAGS:['script','style','iframe','object','embed','form','input','meta','link'],
        FORBID_ATTR:['id','name','form','formaction'],ALLOW_DATA_ATTR:false,ADD_ATTR:['data-latex'],
      });
    }catch{return `<pre class="message-source">${escape(source)}</pre>`;}
  }
  function copyButton(button,text){
    if(button.dataset.bound) return;
    button.dataset.bound='true';
    button.onclick=async()=>{
      const label=button.textContent;
      try{await root.navigator.clipboard.writeText(text());button.textContent='已复制 ✓';}
      catch{button.textContent='复制失败，请选择文本复制';}
      root.setTimeout(()=>{button.textContent=label;},2000);
    };
  }
  function bind(container){
    for(const button of container.querySelectorAll('.code-copy')){
      copyButton(button,()=>button.closest('.code-frame').querySelector('code').textContent);
    }
    for(const formula of container.querySelectorAll('.katex-display')){
      const source=formula.getAttribute('data-latex') || formula.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
      if(!source || formula.parentElement.classList.contains('math-block')) continue;
      const frame=root.document.createElement('div');frame.className='math-block';
      formula.before(frame);frame.append(formula);
      const button=root.document.createElement('button');button.type='button';button.className='math-copy';button.textContent='复制 LaTeX';
      frame.append(button);copyButton(button,()=>source);
    }
  }
  function append(container,source,limit=60000){
    const block=root.document.createElement('div');block.className='markdown-body';
    block.innerHTML=html(source.slice(0,limit));container.append(block);bind(block);
    if(source.length>limit){
      const button=root.document.createElement('button');button.className='button';button.textContent='显示完整 Markdown';
      button.onclick=()=>{block.innerHTML=html(source);bind(block);button.remove();};container.append(button);
    }
  }
  root.ScienceMarkdown={html,bind,append,attachCopy:copyButton};
})(typeof window!=='undefined'?window:globalThis);
