/* Read-only presentation of ATIF tool calls and serialized tool responses. */
(function(root){
  'use strict';

  // Harbor sometimes stores Python repr(list[content block]) inside a JSON string.
  // Parse literals only: no eval, expressions, constructors or attribute access.
  function pythonLiteral(source){
    let position=0,nodes=0;
    const fail=()=>{throw new Error('Not a literal');};
    const space=()=>{while(/\s/.test(source[position]||'') && position<source.length) position++;};
    function string(){
      const quote=source[position++];let result='';
      while(position<source.length){
        let char=source[position++];
        if(char===quote) return result;
        if(char==='\n' || char==='\r') fail();
        if(char!=='\\'){result+=char;continue;}
        char=source[position++];
        const escapes={'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f','v':'\v','a':'\x07','\\':'\\',"'":"'",'"':'"'};
        if(Object.hasOwn(escapes,char)) result+=escapes[char];
        else if(['x','u','U'].includes(char)){
          const length={x:2,u:4,U:8}[char],hex=source.slice(position,position+length);
          if(!new RegExp('^[0-9a-fA-F]{'+length+'}$').test(hex)) fail();
          const point=parseInt(hex,16);if(point>0x10ffff) fail();
          result+=String.fromCodePoint(point);position+=length;
        }else if(/[0-7]/.test(char||'')){
          const octal=(char+source.slice(position)).match(/^[0-7]{1,3}/)[0];
          position+=octal.length-1;result+=String.fromCharCode(parseInt(octal,8));
        }else if(char==='\n'){/* Python line continuation. */}
        else if(char) result+='\\'+char;
        else fail();
      }
      fail();
    }
    function value(depth=0){
      if(depth>32 || ++nodes>50000) fail();space();
      const char=source[position];
      if(char==='"' || char==="'") return string();
      if(char==='[' || char==='{'){
        const array=char==='[',end=array?']':'}',result=array?[]:Object.create(null);position++;space();
        if(source[position]===end){position++;return result;}
        while(position<source.length){
          const item=value(depth+1);space();
          if(array) result.push(item);
          else{
            if(typeof item!=='string' || source[position++]!==':') fail();
            result[item]=value(depth+1);space();
          }
          if(source[position]===end){position++;return result;}
          if(source[position++]!==',') fail();space();
          if(source[position]===end){position++;return result;}
        }
        fail();
      }
      const literal=source.slice(position).match(/^(True|False|None|true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)(?=\s|[,\]}]|$)/);
      if(!literal) fail();position+=literal[0].length;
      return ({True:true,False:false,None:null,true:true,false:false,null:null})[literal[0]] ?? (['None','null'].includes(literal[0])?null:Number(literal[0]));
    }
    const result=value();space();if(position!==source.length) fail();return result;
  }
  function decode(value,context){
    // Do not turn ordinary paths, regexes or printed literal \\n into newlines.
    for(let layer=0;layer<4 && typeof value==='string';layer++){
      const text=value.trim();
      if(text.length>5000000 || !/^[\[{'"]/.test(text)) break;
      let parsed;
      try{parsed=JSON.parse(text);}catch{
        try{
          parsed=pythonLiteral(text);
          if(context && Array.isArray(parsed) && parsed.some(item=>item?.type==='input_text')) context.escapedTransport=true;
        }catch{break;}
      }
      if(parsed===value) break;value=parsed;
    }
    return value;
  }
  function transportText(source){
    // Codex input_text repr envelopes escape the textual tool output once more.
    // This is applied only to leaves of that envelope, never to tool arguments.
    const escapes={n:'\n',r:'\r',t:'\t',b:'\b',f:'\f',v:'\v',a:'\x07','\\':'\\',"'":"'",'"':'"'};
    return source.replace(/\\(\\|n|r|t|b|f|v|a|'|"|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})/g,(_,escaped)=>
      Object.hasOwn(escapes,escaped)?escapes[escaped]:String.fromCharCode(parseInt(escaped.slice(1),16)));
  }
  function languageFor(key,name,args){
    const file=args?.file_path || args?.path || args?.target_file || '';
    const extension=file.split('.').pop().toLowerCase();
    const languages={py:'python',js:'javascript',mjs:'javascript',cjs:'javascript',ts:'typescript',sh:'bash',bash:'bash',json:'json',html:'html',css:'css',md:'markdown',yaml:'yaml',yml:'yaml',cpp:'cpp',c:'c',rs:'rust'};
    if(/^(patch|diff)$/.test(key) || /apply_patch/i.test(name)) return 'diff';
    if(/^(cmd|command|commands)$/.test(key)) return 'bash';
    if(key==='input' && /^(?:functions\.)?exec$/.test(name)) return 'javascript';
    if(/^(code|script|input|content|new_string|old_string)$/.test(key)) return languages[extension] || (/python/i.test(name)?'python':'text');
    return 'text';
  }
  function codeBlock(parent,text,label='text',language='text'){
    text=String(text??'');
    const frame=root.document.createElement('div');frame.className='code-frame';
    const toolbar=root.document.createElement('div');toolbar.className='code-toolbar';
    const title=root.document.createElement('span');title.textContent=label;
    const button=root.document.createElement('button');button.type='button';button.className='code-copy';button.textContent='复制代码';
    toolbar.append(title,button);
    const pre=root.document.createElement('pre'),code=root.document.createElement('code');
    code.className='hljs';pre.append(code);frame.append(toolbar,pre);parent.append(frame);
    function display(source){
      code.textContent=source;
      if(source.length<=20000 && language!=='text' && root.hljs?.getLanguage(language)){
        try{code.innerHTML=root.hljs.highlight(source,{language,ignoreIllegals:true}).value;}catch{/* Literal fallback. */}
      }
    }
    display(text.slice(0,60000));
    root.ScienceMarkdown.attachCopy(button,()=>text);
    if(text.length>60000){
      const more=root.document.createElement('button');more.type='button';more.className='button';
      more.textContent='显示剩余内容 · '+text.length.toLocaleString()+' 字符';
      more.onclick=()=>{display(text);more.remove();};parent.append(more);
    }
  }
  function field(parent,key,value,name,args){
    // Argument strings are code, not serialized response envelopes. Preserve escapes.
    if(typeof value==='string') codeBlock(parent,value,key+' · '+languageFor(key,name,args),languageFor(key,name,args));
    else codeBlock(parent,JSON.stringify(value,null,2),key+' · JSON','json');
  }
  function argumentsView(parent,value,name){
    const args=decode(value);
    if(args && typeof args==='object' && !Array.isArray(args)){
      for(const [key,item] of Object.entries(args)){
        if(item==null || typeof item==='boolean' || typeof item==='number' || (typeof item==='string' && item.length<160 && !item.includes('\n') && !/^(cmd|command|input|code|script|patch|content|new_string|old_string)$/.test(key))){
          const row=root.document.createElement('div');row.className='tool-parameter';
          const label=root.document.createElement('span');label.textContent=key;
          const text=root.document.createElement('code');text.textContent=typeof item==='string'?item:JSON.stringify(item);
          row.append(label,text);parent.append(row);
        }else field(parent,key,item,name,args);
      }
      if(!Object.keys(args).length) codeBlock(parent,'{}','arguments · JSON','json');
    }else if(typeof args==='string') codeBlock(parent,args,'input · '+languageFor('input',name),languageFor('input',name));
    else codeBlock(parent,JSON.stringify(args,null,2),'arguments · JSON','json');
  }
  function metadataView(parent,value){
    const simple=Object.entries(value).filter(([,item])=>item==null || ['string','number','boolean'].includes(typeof item));
    if(simple.length){
      const row=root.document.createElement('div');row.className='tool-metadata';
      for(const [key,item] of simple){
        const field=root.document.createElement('span');field.textContent=key+': '+String(item);row.append(field);
      }
      parent.append(row);
    }
    const nested=Object.fromEntries(Object.entries(value).filter(([,item])=>item!=null && typeof item==='object'));
    if(Object.keys(nested).length) codeBlock(parent,JSON.stringify(nested,null,2),'metadata · JSON','json');
  }
  function responseView(parent,value,depth=0,label='output',escapedTransport=false){
    if(depth>24){codeBlock(parent,JSON.stringify(value,null,2),label+' · JSON','json');return;}
    const context={};value=decode(value,context);escapedTransport ||= Boolean(context.escapedTransport);
    if(value==null || typeof value!=='object'){
      codeBlock(parent,escapedTransport && typeof value==='string'?transportText(value):String(value),label);return;
    }
    if(Array.isArray(value)){
      // Content blocks and multiple exec results have individual, readable outputs.
      if(value.length && value.every(item=>item && typeof item==='object' && !Array.isArray(item))){
        for(const item of value) responseView(parent,item,depth+1,label,escapedTransport);
      }else codeBlock(parent,JSON.stringify(value,null,2),label+' · JSON','json');
      return;
    }
    if(value.type==='image' || value.type==='image_url'){
      const mime=value.source?.media_type || value.file?.type,base64=value.source?.data || value.file?.base64;
      if(/^image\/(png|jpeg|webp|gif)$/.test(mime||'') && typeof base64==='string'){
        const img=root.document.createElement('img');img.alt='Image from original trajectory';img.loading='lazy';img.src=`data:${mime};base64,${base64}`;parent.append(img);
      }else codeBlock(parent,'[图片引用：'+(value.source?.path || '见原始 Harbor 轨迹')+']','image');
      return;
    }
    // Recognize transport envelopes, retaining other fields above their content.
    const keys=Object.keys(value);
    let payload=[];
    if(['text','input_text','output_text'].includes(value.type) && typeof value.text==='string') payload=['text'];
    else if(Object.hasOwn(value,'content')) payload=['content'];
    else if(Array.isArray(value.results)) payload=['results'];
    else if(Object.hasOwn(value,'value') && ['fulfilled','rejected'].includes(value.status)) payload=['value'];
    else payload=keys.filter(key=>['output','stdout','stderr','return','text'].includes(key));
    if(!payload.length){codeBlock(parent,JSON.stringify(value,null,2),label+' · JSON','json');return;}
    const metadata=Object.fromEntries(Object.entries(value).filter(([key])=>!payload.includes(key) && !['type','source_call_id','tool_call_id'].includes(key)));
    if(Object.keys(metadata).length) metadataView(parent,metadata);
    for(const key of payload) responseView(parent,value[key],depth+1,['text','content','results','value'].includes(key)?label:key,escapedTransport);
  }
  function nameOf(tool){return tool.function_name || tool.function?.name || tool.name || 'Tool';}
  function resultsOf(step){
    if(step.observation!=null){
      const observation=decode(step.observation);
      if(Array.isArray(observation?.results)) return observation.results;
      return [{content:observation}];
    }
    if(step.source==='tool' && step.message!=null) return [{source_call_id:step.tool_call_id || step.source_call_id,content:step.message}];
    return [];
  }
  function section(parent,kind,name,id){
    const block=root.document.createElement('section');block.className='tool-block tool-'+kind;
    const heading=root.document.createElement('div');heading.className='tool-heading';
    const label=root.document.createElement('strong');label.textContent=kind==='call'?'Tool call':'Tool response';
    const tool=root.document.createElement('span');tool.textContent=name;heading.append(label,tool);
    if(id) heading.title=id;
    block.append(heading);parent.append(block);return block;
  }
  function appendStep(parent,step,callNames=new Map()){
    const results=resultsOf(step),used=new Set();
    function response(result,name){
      const id=result?.source_call_id || result?.tool_call_id;
      const block=section(parent,'response',name || callNames.get(id) || '',id);
      responseView(block,result?.content ?? result);
    }
    for(const tool of step.tool_calls||[]){
      const name=nameOf(tool),id=tool.tool_call_id || tool.id;
      const block=section(parent,'call',name,id);
      argumentsView(block,tool.arguments ?? tool.function?.arguments ?? tool,name);
      results.forEach((result,index)=>{
        if(id && (result?.source_call_id || result?.tool_call_id)===id){response(result,name);used.add(index);}
      });
    }
    results.forEach((result,index)=>{if(!used.has(index)) response(result);});
  }
  root.ScienceTools={appendStep,codeBlock,decode,nameOf};
})(typeof window!=='undefined'?window:globalThis);
