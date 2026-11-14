import test from 'node:test';
import assert from 'node:assert/strict';
import {tableCsv} from '../web/modules/domain.js';

test('CSV formula-like text and control-prefixed cells stay literal, including column names',()=>{
  const cases=[
    ['=1+1',`"'=1+1"`],
    ['+1+1',`"'+1+1"`],
    ['-1+2',`"'-1+2"`],
    ['@SUM(1,2)',`"'@SUM(1,2)"`],
    ['  =1+1',`"'  =1+1"`],
    ['\n+1+1',`"'\n+1+1"`],
    ['\tSample A',`"'\tSample A"`],
    ['\rSample A',`"'\rSample A"`],
    ['=CONCAT("A","B")',`"'=CONCAT(""A"",""B"")"`],
    ["'=1+1",`"'=1+1"`],
  ];
  for(const [input,expected] of cases)assert.equal(tableCsv([{sample:input}]),'\ufeff"sample"\r\n'+expected,JSON.stringify(input));
  assert.equal(tableCsv([{'=1+1':'@SUM(1,2)'}]),`\ufeff"'=1+1"\r\n"'@SUM(1,2)"`);
});

test('CSV preserves scientific number precision and signs while treating signed strings as text',()=>{
  const rows=[{raw:-1.2345678901234567},{raw:1.2345678901234567},{raw:-1e-300},
    {raw:5e-324},{raw:9007199254740991},{raw:0},{raw:-0},{raw:'-1.25'},{raw:'+1.25'}];
  assert.equal(tableCsv(rows),[
    '\ufeff"raw"','"-1.2345678901234567"','"1.2345678901234567"','"-1e-300"',
    '"5e-324"','"9007199254740991"','"0"','"0"',`"'-1.25"`,`"'+1.25"`,
  ].join('\r\n'));
});

test('CSV retains nulls, missing cells, structured values, normal text and original data unchanged',()=>{
  const rows=[{sample:'Control, "A"\r\nreplicate 1',empty:null,missing:undefined,
    values:[-1.25,'=1+1',null],metadata:{name:'+control'},included:false},
    {sample:'β-actin 10⁻⁶',included:true}];
  const original=structuredClone(rows);
  assert.equal(tableCsv(rows),[
    '\ufeff"sample","empty","missing","values","metadata","included"',
    '"Control, ""A""\r\nreplicate 1","","","[-1.25,""=1+1"",null]","{""name"":""+control""}","false"',
    '"β-actin 10⁻⁶","","","","","true"',
  ].join('\r\n'));
  assert.deepEqual(rows,original);
  assert.equal(tableCsv([]),'');assert.equal(tableCsv(null),'');
});
