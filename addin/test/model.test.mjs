import test from 'node:test';
import assert from 'node:assert/strict';
import {Selection, actionPayload, currentMessageId} from '../src/model.mjs';

test('actions bind to the displayed ticket version',()=>{
 assert.deepEqual(actionPayload({version:7,actions:['approve']},'approve'),{action:'approve',version:7,reason:''});
 assert.throws(()=>actionPayload({version:7,actions:[]},'approve'));
});
test('routing/rejection/closure/reopen require reasons',()=>{
 for(const action of ['route','reject','close','reopen']) assert.throws(()=>actionPayload({version:1,actions:[action]},action,' '));
 assert.equal(actionPayload({version:1,actions:['route']},'route','Student clarified','MIGRATION').category,'MIGRATION');
});
test('selection generations reject stale asynchronous email results',()=>{
 const selection=new Selection(),a=selection.next(),b=selection.next();
 assert.equal(selection.isCurrent(a),false);assert.equal(selection.isCurrent(b),true);
});
test('uses stable email ID and handles no item context',()=>{
 assert.equal(currentMessageId({context:{mailbox:{item:null}}}),null);
 assert.equal(currentMessageId({context:{mailbox:{item:{internetMessageId:'<mail@example.org>',itemId:'unstable-id'}}}}),'<mail@example.org>');
});
