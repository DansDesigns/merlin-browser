const e = React.createElement;
function App() {
  const [count, setCount] = React.useState(0);
  const [repos, setRepos] = React.useState([]);
  const [text, setText] = React.useState("");
  const [sent, setSent] = React.useState("");
  React.useEffect(() => { fetch('/repos.json').then(r => r.json()).then(setRepos); }, []);
  return e('div', {id: 'app'},
    e('button', {id: 'count', style: {padding: '10px 20px'}, onClick: () => setCount(c => c + 1)}, 'Clicked ' + count + ' times'),
    e('ul', {id: 'repos'}, repos.map(r => e('li', {key: r.name}, r.name))),
    e('form', {id: 'f', onSubmit: ev => { ev.preventDefault(); setSent(text); }},
      e('input', {id: 'q', name: 'q', value: text, onChange: ev => setText(ev.target.value)}),
      e('p', {id: 'echo'}, 'typed: ' + text), e('p', {id: 'sent'}, 'sent: ' + sent)));
}
ReactDOM.hydrateRoot(document.getElementById('root'), e(App));
