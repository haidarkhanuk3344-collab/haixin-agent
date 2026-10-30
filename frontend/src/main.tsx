import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './style.css';
class Boundary extends React.Component<{children:React.ReactNode},{failed:boolean}>{state={failed:false};static getDerivedStateFromError(){return {failed:true}}render(){return this.state.failed?<main className="recovery"><h1>页面遇到一点问题</h1><p>后台任务和已保存的内容仍在。请刷新页面重新连接。</p><button onClick={()=>location.reload()}>重新打开</button></main>:this.props.children}}
createRoot(document.getElementById('root')!).render(<Boundary><App/></Boundary>);
