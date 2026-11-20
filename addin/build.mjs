import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
import {mkdir, copyFile, writeFile} from 'node:fs/promises';
import {deflateSync} from 'node:zlib';
const output = new URL('../alumni/static/', import.meta.url);
await mkdir(output, {recursive:true});
await build({entryPoints:['src/taskpane.js'],bundle:true,minify:true,target:['es2022'],outfile:fileURLToPath(new URL('taskpane.js',output))});
for (const name of ['taskpane.html','taskpane.css','auth.html']) await copyFile(new URL('src/'+name,import.meta.url),new URL(name,output));
// Small code-generated PNG monogram assets; no external images or branding guesses.
function crc(bytes) { let c=0xffffffff; for(const byte of bytes){c^=byte;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);} return (c^0xffffffff)>>>0; }
function chunk(type,data){const name=Buffer.from(type),body=Buffer.concat([name,data]),len=Buffer.alloc(4),sum=Buffer.alloc(4);len.writeUInt32BE(data.length);sum.writeUInt32BE(crc(body));return Buffer.concat([len,body,sum]);}
for(const size of [16,32,80,128]){
 const header=Buffer.alloc(13);header.writeUInt32BE(size);header.writeUInt32BE(size,4);header[8]=8;header[9]=2;
 const rows=[];for(let y=0;y<size;y++){const row=Buffer.alloc(1+size*3);for(let x=0;x<size;x++){const stroke=(x>size*.23&&x<size*.36&&y>size*.22&&y<size*.73)||(x>size*.64&&x<size*.77&&y>size*.22&&y<size*.73)||(y>size*.64&&y<size*.78&&x>size*.23&&x<size*.77);row.set(stroke?[255,255,255]:[18,62,56],1+x*3);}rows.push(row);}
 await writeFile(new URL(`icon-${size}.png`,output),Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(Buffer.concat(rows))),chunk('IEND',Buffer.alloc(0))]));
}
