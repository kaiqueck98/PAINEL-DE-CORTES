(() => {
  const form = document.querySelector('form [name="folder_link"]')?.form;
  if (form) {
    for (const name of ['title_template', 'hashtags_template']) {
      const input = form.querySelector('[name="' + name + '"]');
      if (input) {
        input.value = '';
        input.disabled = true;
        input.placeholder = 'Gerado individualmente a partir do conteúdo do vídeo';
      }
    }
  }
  const host = document.createElement('section');
  host.className = 'card';
  const heading = document.createElement('h3');
  heading.textContent = 'Histórias e seleção do dia';
  host.append(heading);
  const body = document.createElement('div'); host.append(body);
  (document.querySelector('main') || document.body).append(host);
  let editing = false;
  async function refreshContent() {
    if(editing) {setTimeout(refreshContent,5000); return;}
    try {
      const response = await fetch('__CONTENT_ENDPOINT__', {headers: {
        'X-CSRF-Token': document.querySelector('[name="csrf_token"]')?.value || ''
      }, cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Erro ao consultar análise');
      const history = document.getElementById('upload-history');
      if (history) {
        history.replaceChildren();
        for (const row of data.history) {
          const p = document.createElement('p');
          p.textContent = `${row.run_date} ${row.run_time} | ${row.video_name} | ${row.platform} | ${row.status}: ${row.message || 'Aguardando'}`;
          history.append(p);
        }
        if (!data.history.length) history.textContent = 'Ainda não há envios.';
      }
      body.replaceChildren();
      for(const event of (data.notifications || [])) {
        const p=document.createElement('p');p.className='notice';p.textContent=event.message+' — '+event.result;body.append(p);
      }
      for (const warning of (data.warnings || [])) {
        const p = document.createElement('p'); p.className = 'notice'; p.textContent = warning; body.append(p);
      }
      for (const day of data.content.days) {
        const p = document.createElement('p'); p.textContent = day.status + ': ' + day.message; body.append(p);
      }
      for (const review of (data.reviews || [])) {
        const section = document.createElement('section'); section.className = 'card';
        const header = document.createElement('h3');
        header.textContent = review.status === 'confirmed' ? 'Lote confirmado · ' + review.target_date : 'Prévia — aprove os quatro vídeos';
        section.append(header);
        const notice = document.createElement('p'); notice.setAttribute('role', 'status'); section.append(notice);
        async function act(action, video, button) {
          editing=true; button.disabled = true; notice.textContent = 'Processando…';
          try {
            const r = await fetch('/agendamento/revisar', {method:'POST', headers:{'Content-Type':'application/json',
              'X-CSRF-Token': document.querySelector('[name="csrf_token"]')?.value || ''},
              body:JSON.stringify({schedule_id:review.schedule_id, revision:review.revision, action, video_id:video?.id})});
            const result = await r.json(); if (!r.ok) throw new Error(result.error || 'Falha ao atualizar');
            notice.textContent = result.message; location.reload();
          } catch(e) { notice.textContent=e.message; button.disabled=false; editing=false; }
        }
        for (const video of review.state.videos) {
          const card = document.createElement('article'); card.className='card';
          const copy=video.content || {};
          if(video.thumbnail) { const img=document.createElement('img');
            img.src='__COVER_ENDPOINT__'.replace('SHA',video.sha256)+'?revision='+review.revision;
            img.alt='Capa: '+copy.title; card.append(img); }
          for(const line of ['História detectada: '+copy.summary,
            'Título no vídeo: '+(copy.japanese ? copy.japanese.text+' ('+copy.japanese.translation+')' : 'Pendente'),
            'Emoção escolhida: '+(copy.japanese?.reason || copy.japanese_error || ''), 'Título para postar: '+copy.title,
            'Legenda: '+copy.caption, 'Hashtags: '+(copy.hashtags||[]).join(' '),
            'Nota: '+video.score+'/10 — '+(video.reasons||[]).join('; ')]) {
            const p=document.createElement('p');p.textContent=line;card.append(p);
          }
          if(video.japanese_variant) {
            const caption=document.createElement('p');caption.textContent='Prévia do título nos primeiros 2 segundos';card.append(caption);
            const image=document.createElement('img');image.src='/agendamento/titulo-japones/'+video.sha256+'/'+video.japanese_variant;
            image.alt='Título no vídeo: '+(copy.japanese?.text || '');image.style.width='360px';card.append(image);
          }
          const risk=video.risk || {checked:false, reasons:[]};
          const limitation=document.createElement('small');limitation.textContent=risk.limitation||'Triagem amostral; não comprova direitos autorais.';card.append(limitation);
          for(const reason of risk.reasons || []) {const p=document.createElement('p');p.style.color='#ff8585';
            p.textContent='RISCO: '+reason;card.append(p);}
          if(!risk.checked){const p=document.createElement('p');p.style.color='#ff8585';
            p.textContent='Verificação incompleta: '+(risk.warning||'aguardando triagem');card.append(p);}
          if(review.status==='draft') {
            for(const [action,label] of [['approve','APROVAR'],['title','TROCAR TÍTULO'],['japanese','TROCAR TÍTULO JAPONÊS'],['replace','TROCAR VÍDEO']]) {
              const b=document.createElement('button');b.type='button';b.textContent=label;b.style.margin='4px';
              if(action==='approve') {b.disabled=!risk.checked || !!risk.reasons.length || (copy.japanese_required && (!copy.japanese || video.japanese_rendered!==copy.japanese.text)) || review.state.approved.includes(video.id);
                if(review.state.approved.includes(video.id)) b.textContent='APROVADO';}
              b.onclick=()=>act(action,video,b);card.append(b);
            }
          }
          const b=document.createElement('button');b.type='button';b.textContent='Copiar título e hashtags';
          b.onclick=async()=>{try{await navigator.clipboard.writeText(copy.title+'\n'+(copy.hashtags||[]).join(' '));b.textContent='Copiado';}
            catch(_){b.textContent='Selecione e copie os textos acima';}};card.append(b);section.append(card);
        }
        if(review.status==='draft' && review.state.videos.length===4 && review.state.videos.every(v=>review.state.approved.includes(v.id))) {
          const p=document.createElement('p');p.textContent='Horários: 08:00, 12:30, 18:00 e 21:00 (São Paulo). Após 08:00, o lote será agendado para amanhã.';section.append(p);
          const b=document.createElement('button');b.textContent='CONFIRMAR E AGENDAR';b.onclick=()=>act('confirm',null,b);section.append(b);
        } else if(review.status==='draft' && review.state.videos.length<4) {
          const p=document.createElement('p');p.textContent='São necessários quatro vídeos para confirmar. Adicione mais vídeos e crie uma nova prévia.';section.append(p);
        }
        body.append(section);
      }
      for (const video of data.content.videos) {
        const card = document.createElement('article'); card.className = 'card';
        const copy = video.content || {};
        const score = video.score_scale === 10 ? video.score : video.score / 10;
        const lines = [video.name + ' | Nota: ' + score.toFixed(1) + '/10 | ' + (video.selected ? 'ESCOLHIDO' : video.status),
          'História detectada: ' + (copy.summary || 'Análise ainda indisponível'),
          'Título no vídeo: ' + (copy.japanese ? copy.japanese.text+' ('+copy.japanese.translation+')' : (copy.japanese_error || 'Indisponível')),
          'Título para postar: ' + (copy.title || ''), 'Legenda: ' + (copy.caption || ''),
          'Hashtags: ' + (copy.hashtags || []).join(' '), video.message,
          'Motivos: ' + (video.reasons || []).join('; ')];
        if (copy.uncertainty) lines.push('Limitações: ' + copy.uncertainty);
        if (copy.warning) lines.push('Aviso: ' + copy.warning);
        if (video.asset_warning) lines.push(video.asset_warning);
        if (copy.learning?.basis) lines.push(copy.learning.basis);
        for (const [tag, reason] of Object.entries(copy.hashtag_reasons || {})) {
          const source = (copy.hashtag_sources || {})[tag] || {};
          lines.push(`${tag}: ${reason} | ${source.evidence || ''} | ${source.source || ''} | ${source.measured_at || ''}`);
        }
        if (copy.trend_source) lines.push('Lista de tendências: ' + copy.trend_source + ' | ' + copy.trend_updated_at);
        for (const line of lines) { const p = document.createElement('p'); p.textContent = line; card.append(p); }
        if (video.thumbnail && video.sha256) {
          const img = document.createElement('img'); img.src = '__COVER_ENDPOINT__'.replace('SHA', video.sha256);
          img.alt = 'Capa automática: ' + (copy.title || ''); img.loading = 'lazy'; card.append(img);
        }
        if ((video.steps || []).length) {
          const details = document.createElement('details'); details.className = 'steps';
          const summary = document.createElement('summary'); summary.textContent = 'Passo a passo'; details.append(summary);
          for (const text of video.steps) {const p = document.createElement('p'); p.textContent = text; details.append(p);}
          card.append(details);
        }
        if (copy.title) {
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = 'Copiar título e hashtags';
          button.onclick = async () => {
            try { await navigator.clipboard.writeText(copy.title + '\n' + copy.hashtags.join(' ')); button.textContent = 'Copiado'; }
            catch (_) { button.textContent = 'Selecione e copie os textos acima'; }
          };
          card.append(button);
        }
        body.append(card);
      }
      if (!data.content.days.length) {const p = document.createElement('p');p.textContent='Clique em RODAR AUTOMÁTICO para começar a análise.';body.append(p);}
    } catch (error) { body.textContent = error.message; }
    finally { setTimeout(refreshContent, 5000); }
  }
  refreshContent();
})();
