const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/Menu0001.js"])))=>i.map(i=>d[i]);
var a={params:{id:`fav-id`,metadata:{},name:`FavoriteTweet`,operationKind:`mutation`,text:null}};
var b={params:{id:`unfav-id`,metadata:{},name:`likeButtonControlUnfavoriteMutation`,operationKind:`mutation`,text:null}};
var c={params:{id:`rt-id`,metadata:{},name:`CreateRetweet`,operationKind:`mutation`,text:null,providedVariables:{p:q}}};
var d={params:{id:`unrt-id`,metadata:{},name:`retweetButtonUnretweetMutation`,operationKind:`mutation`,text:null}};
var e={params:{id:`bm-id`,metadata:{},name:`BookmarkAdd`,operationKind:`mutation`,text:null}};
var f={params:{id:`unbm-id`,metadata:{},name:`BookmarkDelete`,operationKind:`mutation`,text:null}};
var g={params:{id:`post-id`,metadata:{},name:`CreateTweet`,operationKind:`mutation`,text:null}};
async function s({userId:t}){await Ct(`/1.1/friendships/create.json`,{method:`POST`,body:C({user_id:t})})}
async function d2({userId:t}){await Ct(`/1.1/friendships/destroy.json`,{method:`POST`,body:C({user_id:t})})}
function menu(){return import(`./Menu0001.js`),__vite__mapDeps([0])}
