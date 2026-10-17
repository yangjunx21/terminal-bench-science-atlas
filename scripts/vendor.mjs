import {readFile,writeFile,mkdir,copyFile,cp,readdir} from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const packages=[
  ['markdown-it','markdown-it',[['dist/browser/markdown-it.umd.min.js','markdown-it.min.js']],'LICENSE'],
  ['markdown-it-texmath','texmath',['texmath.js','css/texmath.css'],'license.txt'],
  ['katex','katex',['dist/katex.min.js','dist/katex.min.css'],'LICENSE'],
  ['dompurify','dompurify',['dist/purify.min.js'],'LICENSE'],
  ['@highlightjs/cdn-assets','highlight',['highlight.min.js','styles/github.min.css'],'LICENSE'],
];
const manifest=[];
for(const [name,folder,files,license] of packages){
  const src=path.join(root,'node_modules',name),dst=path.join(root,'web/vendor',folder);
  await mkdir(dst,{recursive:true});
  const metadata=JSON.parse(await readFile(path.join(src,'package.json'),'utf8'));
  for(const item of files){
    const [file,target]=Array.isArray(item)?item:[item,path.basename(item)];
    await copyFile(path.join(src,file),path.join(dst,target));
  }
  await copyFile(path.join(src,license),path.join(dst,'LICENSE'));
  if(name==='katex') await cp(path.join(src,'dist/fonts'),path.join(dst,'fonts'),{recursive:true});
  manifest.push({name,version:metadata.version,license:metadata.license});
}
async function inventory(directory){
  const rows=[];
  for(const item of await readdir(directory,{withFileTypes:true})){
    const file=path.join(directory,item.name);
    if(file===path.join(root,'web/vendor/manifest.json')) continue;
    if(item.isDirectory()) rows.push(...await inventory(file));
    else rows.push({path:path.relative(path.join(root,'web/vendor'),file),sha256:createHash('sha256').update(await readFile(file)).digest('hex')});
  }
  return rows;
}
await writeFile(path.join(root,'web/vendor/manifest.json'),JSON.stringify({packages:manifest,files:await inventory(path.join(root,'web/vendor'))},null,2)+'\n');
console.log('Vendored offline rendering dependencies:',manifest.map(p=>`${p.name}@${p.version}`).join(', '));
