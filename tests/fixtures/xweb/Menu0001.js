async function p({userId:t}){await Se(`/1.1/blocks/create.json`,{method:`POST`})}
async function q({userId:t}){await Se(`/1.1/blocks/destroy.json`,{method:`POST`})}
async function w({userId:t}){await C(`/1.1/mutes/users/create.json`,{method:`POST`})}
async function x({userId:t}){await C(`/1.1/mutes/users/destroy.json`,{method:`POST`})}
