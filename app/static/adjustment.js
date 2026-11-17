const feedback = document.querySelector('#feedback');
const adjustForm = document.querySelector('#adjust-form');
async function postJSON(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || '调整失败，原账已保留');
  return result;
}
adjustForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = adjustForm.querySelector('button');
  button.disabled = true;
  button.textContent = '生成中';
  try {
    const result = await postJSON('/api/adjustments', {
      run_id: adjustForm.dataset.run,
      new_standard_moisture: adjustForm.elements.new_standard_moisture.value,
    });
    location.assign(`/m/6?run=${result.run_id}&adj=${encodeURIComponent(result.id)}`);
  } catch (error) {
    feedback.textContent = error.message;
    button.disabled = false;
    button.textContent = '生成新版本结算';
  }
});
document.querySelectorAll('.adjust-resume').forEach((button) => {
  button.addEventListener('click', async () => {
    const card = button.closest('.adjust-card');
    button.disabled = true;
    button.textContent = '续跑中';
    try {
      const result = await postJSON(
        `/api/adjustments/${card.dataset.adj}/resume`, {});
      location.assign(`/m/6?run=${result.run_id}&adj=${encodeURIComponent(result.id)}`);
    } catch (error) {
      feedback.textContent = error.message;
      location.reload();
    }
  });
});
