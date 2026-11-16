import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(process.argv[2] || path.join(root, '.build'));
await fs.mkdir(out, { recursive: true });
const data = JSON.parse(await fs.readFile(path.join(root, 'data.json'), 'utf8'));
const p = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const C = { ink:'#F1F6F0', paper:'#0C211C', muted:'#B5CBBE', green:'#8BC6AC', amber:'#36594D', line:'#36594D', white:'#F1F6F0', lime:'#9BD1B7', dark:'#0C211C', lightMuted:'#B5CBBE' };
const font = 'Arial';
const site = 'https://ai-builder-lab.com/';
const compare = `${site}compare?models=23c49394cadf4e0ab3b7f78d9d70373a,03b5d22868b44bbb975d3cfc01d46f93&run=${data.run_id}&revision=${data.revision_id}`;
const notes = [
  'Я создал и развернул сайт для исследования бизнес-моделей. Он помогает выбрать, какую идею проверить платным тестом за девяносто дней. В каталоге двадцать моделей. Пять получили условные оценки после проверки агентами, пятнадцать требуют дополнительного исследования.',
  'Исследование работает на сервере Aeza: система собирает источники, проверяет обязательные критерии, запускает критика и проверяющего, затем формирует карточку. PostgreSQL хранит состояние, результаты и расходы. Прерванную работу можно продолжить. Сейчас запуск выполняет оператор на сервере, а сайт позволяет просматривать результаты.',
  'Здесь показаны все двадцать моделей по десяти обязательным факторам. Светло-зелёным отмечено подтверждение, тёмно-зелёным — нехватка оснований. Явных отказов в текущих карточках нет. Главные пробелы — законность конкретного запуска и необходимость собственной лицензии. Это открытые вопросы, поэтому система сохраняет их рядом с оценкой, а не скрывает. Одна модель может иметь несколько пробелов.',
  'Критик ищет причины, почему запуск может не состояться. Проверяющий получает нейтральные тезисы и документы без оценок и бизнес-выводов критика. Утверждения связаны с источниками и паспортами. Оба этапа реально выполнены для пяти оценённых моделей. Например, для туристических рефералов остаётся риск, что партнёрское одобрение не удастся получить вовремя.',
  'На сайте можно выбрать до трёх карточек одной версии и сравнить их рядом. Здесь электронное подписание и туристические рефералы. Видны покупатель, выручка, оценка, бюджет и время до оплаты. У рефералов оценка выше, но срок может выйти за девяносто дней. Поэтому решение требует сравнения условий, а не только числа. Из таблицы можно перейти к рискам и источникам. Условная оценка означает завершённый анализ с открытыми условиями запуска.',
  'Половину оценки определяют доступ к покупателям и свидетельства готовности платить. Остальное — подготовка предложения, срок до оплаты и ресурсы. Веса находятся в конфигурации. Все варианты оцениваются для команды из двух человек с бюджетом пятнадцать тысяч долларов без зарплат. Диапазоны отражают неопределённость. Балл не является вероятностью успеха.',
  'Дальше я бы выбрал одну модель, закрыл юридические и партнёрские условия и проверил спрос платным пилотом. В полной версии добавил бы кабинет владельца и историю экспериментов. Из задания убрал бы точную вероятность без статистики запусков, а обязательный размер аналога предложил бы пересмотреть. Сайт и исходный код доступны по ссылкам.'
];
const titles = ['Конвейер проверки\nбизнес-моделей','Сайт и серверный конвейер','Обязательные факторы F01–F10','Критик и независимый проверяющий','Сравнение карточек на сайте','Оценка готовности к первой оплате','Следующий шаг: платный пилот'];
const timings = ['0:00–0:15','0:15–0:35','0:35–1:05','1:05–1:30','1:30–2:10','2:10–2:30','2:30–2:50'];
function text(s,t,x,y,w,h,size=26,color=C.ink,bold=false,link=null){
 const q=s.shapes.add({geometry:'textbox',name:t.slice(0,65),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 q.text=t;q.text.style={fontFamily:font,fontSize:size,color,bold,insets:0,verticalAlignment:'top',wrap:true};
 if(link)q.hyperlink={uri:link,isExternal:true};return q;
}
function slide(i,bg=C.paper){
 const s=p.slides.add();s.background.fill=bg;
 const sources=[data.source_url,'https://github.com/bcmle/test_ai_builder',site+'methodology'];
 s.speakerNotes.text=`${timings[i]}\n\n${notes[i]}\n\nИсточники: ${sources.join('\n')}\nСрез: ${data.published_at}; revision ${data.revision_id}.`;
 return s;
}
function folio(s,i,dark=false,label='AI BUILDER'){
 text(s,label,64,673,1050,25,15,dark?C.lightMuted:C.muted,false,label.startsWith('ai-builder-lab.com')?(i===4?compare:site):null);
 text(s,String(i+1).padStart(2,'0'),1174,670,50,29,19,dark?C.lime:C.ink,true);
}
function kicker(s,t,dark=false){text(s,t,64,40,1120,30,18,dark?C.lime:C.green,true);}
async function image(s,name,x,y,w,h,fit='contain'){
 const b=await fs.readFile(path.join(root,'assets',name));
 s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:'image/png',alt:name==='optical-filter.png'?'Концептуальный образ оптического фильтра':name==='home.png'?'Опубликованный сайт AI Builder':'Опубликованная страница сравнения карточек',fit,position:{left:x,top:y,width:w,height:h}});
}
function table(s,vals,x,y,width,height,widths){
 const t=s.tables.add({rows:vals.length,columns:vals[0].length,left:x,top:y,width,height,columnWidths:widths,values:vals});
 t.borders.assign({fill:C.paper,width:0});
 for(let r=0;r<vals.length;r++){
  t.rows[r].height=height/vals.length;
  for(let c=0;c<vals[0].length;c++){
   const q=t.getCell(r,c);q.fill=r===0?'#173C30':r===1?'#214C3D':C.paper;
   q.text.style={fontFamily:font,fontSize:r===1&&c>0?35:r===0?24:26,color:r===0?C.white:C.ink,bold:r===0||r===1};
  }
 }
 return t;
}

let s=slide(0,C.dark);
await image(s,'optical-filter.png',0,0,1280,720,'cover');
kicker(s,'AI BUILDER',true);
text(s,'Проверка\nбизнес-моделей',64,130,880,200,76,C.white,true);
text(s,'Сайт для выбора идеи\nи платного теста за 90 дней',68,352,660,105,31,C.lightMuted);
for(const [x,n,l] of [[64,'20','моделей'],[310,'5','условных оценок'],[590,'15','требуют проверки']]){
 text(s,n,x,514,210,91,75,C.lime,true);text(s,l,x+2,607,270,34,23,C.white);
}
folio(s,0,true,'ai-builder-lab.com');
s.speakerNotes.text+='\nФоновый концептуальный образ создан встроенным ImageGen. Это декоративная иллюстрация, не изображение оборудования проекта.';

s=slide(1);
kicker(s,'РАБОТАЮЩИЙ ПРОДУКТ');
text(s,'Сайт и сервер\nс базой данных',64,94,560,146,55,C.ink,true);
text(s,'ai-builder-lab.com',64,263,470,42,30,C.green,true,site);
await image(s,'home.png',528,109,688,387);
text(s,'Aeza',64,345,350,46,30,C.ink,true);
text(s,'AI-агенты работают\nна сервере',64,396,375,72,25,C.muted);
text(s,'PostgreSQL',64,493,400,45,30,C.ink,true);
text(s,'Состояние, результаты, расходы\nи продолжение после остановки',64,545,450,78,23,C.muted);
text(s,'Источники  /  Отбор  /  Критик',555,525,635,37,24,C.ink,true);
text(s,'Проверка  /  Оценка  /  Карточка',555,566,650,38,24,C.ink,true);
text(s,'HTTPS. Просмотр и сравнение на сайте.\nИсследование запускает оператор на сервере.',555,615,650,51,19,C.muted);
folio(s,1);

s=slide(2);
kicker(s,'ОБЯЗАТЕЛЬНЫЕ ФАКТОРЫ');
text(s,'Где не хватает доказательств',64,87,1160,72,52,C.ink,true);
text(s,'20 моделей в каталоге. Явных отказов в текущем срезе: 0.',64,162,1130,43,25,C.muted);
const criteria=[...data.criteria].reverse();
s.charts.add('bar',{
 position:{left:60,top:213,width:1156,height:388},categories:criteria.map(v=>`${v.id}  ${v.label}`),
 series:[{name:'Подтверждено',values:criteria.map(v=>v.passed),fill:C.green,dataLabels:{showValue:true,position:'center',numberFormatCode:'0;-0;;',textStyle:{typeface:font,fontSize:20,bold:true,fill:C.dark}}},{name:'Не подтверждено',values:criteria.map(v=>v.unknown),fill:C.amber}],
 barOptions:{direction:'bar',grouping:'stacked',gapWidth:76,overlap:100},hasLegend:true,
 legend:{position:'bottom',textStyle:{typeface:font,fontSize:20,fill:C.ink}},
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:20,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:20,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'center',numberFormatCode:'0;-0;;',textStyle:{typeface:font,fontSize:20,bold:true,fill:C.ink}},chartFill:C.paper,plotAreaFill:C.paper
});
text(s,'F01 и F03 остаются открытыми у всех 20 моделей',64,618,1150,38,28,C.ink,true);
folio(s,2,false,'16.11.2026. Включены предыдущие оценки. Одна модель может иметь несколько пробелов.');

s=slide(3,C.dark);
kicker(s,'ПРОВЕРКА ВЫВОДОВ',true);
text(s,'Две роли с раздельными контекстами',64,91,1152,77,51,C.white,true);
text(s,'01',64,207,180,88,69,C.lime,true);
text(s,'Критик',64,310,490,57,39,C.white,true);
text(s,'Что помешает запуску\nи первой оплате?',64,386,505,96,30,C.lightMuted);
text(s,'02',682,207,180,88,69,C.lime,true);
text(s,'Проверяющий',682,310,534,57,39,C.white,true);
text(s,'Нейтральные тезисы и документы.\nБез оценок и бизнес-выводов\nкритика.',682,386,540,122,27,C.lightMuted);
text(s,'5',64,538,95,106,87,C.lime,true);
text(s,'моделей прошли оба этапа\nна реальных данных',178,558,470,83,26,C.white);
text(s,'Паспорт утверждения',682,551,530,40,27,C.white,true);
text(s,'Источник, дата, метод, уверенность',682,602,535,37,23,C.lightMuted);
folio(s,3,true);

s=slide(4);
kicker(s,'ИНСТРУМЕНТ ДЛЯ СОБСТВЕННИКА');
text(s,'Карточки, которые\nможно сравнить',64,88,780,147,55,C.ink,true);
text(s,'До трёх моделей одной версии исследования',64,253,780,40,25,C.muted);
await image(s,'compare-results.png',878,80,338,190);
table(s,[['Параметр','Электронное подписание\nАналог: Docusign','Туристические рефералы\nАналог: KAYAK'],['Оценка / 100','56,25–66,25','62,5–72,5'],['До первой оплаты','43–91 день','45–110 дней'],['Бюджет теста','$4 000–12 000','$4 600–9 700']],64,326,1152,292,[330,411,411]);
text(s,'Оценки условные. У обеих моделей открыты F01 и F03.',64,634,1140,34,23,C.green,true);
folio(s,4,false,'ai-builder-lab.com/compare');

s.speakerNotes.text+=`\nСтраница сравнения: ${compare}\nКомпании — аналоги предлагаемых моделей. Сроки и бюджеты — аналитические оценки. В рисках туристических рефералов отмечено возможное ожидание партнёрского одобрения.\nСсылка для демонстрации: ${compare}`;

s=slide(5,C.dark);
kicker(s,'ЛОГИКА ОЦЕНКИ');
text(s,'Оценка под конкретный запуск',64,88,1150,75,52,C.ink,true);
text(s,'90',58,213,365,174,152,C.ink,true);
text(s,'дней до первой оплаты',65,395,390,85,31,C.ink,true);
text(s,'2 человека\n$15 000 без зарплат\nРынок США',65,502,390,118,26,C.ink);
const factors=[...data.factors].reverse();
s.charts.add('bar',{
 position:{left:470,top:217,width:745,height:369},categories:factors.map(v=>v.label),series:[{name:'Вес',values:factors.map(v=>v.weight),fill:C.green}],
 barOptions:{direction:'bar',grouping:'clustered',gapWidth:125},hasLegend:false,
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:22,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:31,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'outEnd',numberFormatCode:'0"%"',textStyle:{typeface:font,fontSize:26,bold:true,fill:C.ink}},chartFill:C.dark,plotAreaFill:C.dark
});
text(s,'Веса в конфигурации. Диапазоны сохраняют неопределённость.',470,611,746,56,22,C.ink);
folio(s,5,false,'Балл не является статистической вероятностью успеха.');

s=slide(6,C.dark);
kicker(s,'РАЗВИТИЕ ПРОДУКТА',true);
text(s,'Следующая проверка:\nреальный клиент',64,92,1160,145,58,C.white,true);
text(s,'01',64,302,110,73,58,C.lime,true);
text(s,'Платный пилот',204,305,650,56,38,C.white,true);
text(s,'Выбрать модель и закрыть условия запуска',204,375,950,45,28,C.lightMuted);
text(s,'02',64,449,110,73,58,C.lime,true);
text(s,'Полная версия',204,453,650,56,38,C.white,true);
text(s,'Кабинет владельца и история экспериментов',204,523,950,45,28,C.lightMuted);
text(s,'Пересмотрел бы в ТЗ:',64,597,365,40,25,C.lime,true);
text(s,'точную вероятность без статистики\nи обязательность аналога от $300 млн',447,596,765,70,24,C.white);
folio(s,6,true,'ai-builder-lab.com      /      github.com/bcmle/test_ai_builder');

await (await PresentationFile.exportPptx(p)).save(path.join(out,'candidate.pptx'));
for(let i=0;i<p.slides.items.length;i++){
 const b=await p.export({slide:p.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(out,`slide-${i+1}.png`),new Uint8Array(await b.arrayBuffer()));
}
await fs.writeFile(path.join(out,'speaker-notes.md'),'# Сценарий защиты: 2 минуты 50 секунд\n\n'+notes.map((v,i)=>`## ${i+1}. ${titles[i].replaceAll('\n',' ')} (${timings[i]})\n\n${v}`).join('\n\n')+'\n');
console.log(JSON.stringify({slides:p.slides.items.length,out,spokenWords:notes.join(' ').split(/\s+/).length}));
