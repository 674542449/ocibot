/**
 * 把一个 Blob 作为文件下载。
 *
 * 链接要先挂到文档上再点，URL 也要晚一点再回收：游离的 <a> 在部分 Firefox 版本里
 * 点了没反应，而同步 revokeObjectURL 在 Safari 上可能赶在下载真正开始之前就把
 * 地址作废。实例 CSV 和加密备份两处下载共用这一个实现。
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** Read a local text file picked by the user into a string. */
export function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'))
    reader.readAsText(file)
  })
}

export async function pickAndReadTextFile(accept = '.pem,.key,.txt,.pub,*/*'): Promise<string | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    input.style.display = 'none'

    // 只结算一次，且无论走哪条路都要把 input 摘下来。
    //
    // 原来只挂了 onchange：在系统文件框里按 Esc（取消）时浏览器只发 cancel，
    // 于是这个 Promise 永远不 settle —— await 它的调用方就此挂死，谁在 await
    // 之前开了 spinner，那个 spinner 就再也关不掉；同时那个隐藏 input 一直挂在
    // document.body 上，取消几次就攒几个游离节点。
    let settled = false
    const finish = (value: string | null) => {
      if (settled) return
      settled = true
      input.remove()
      resolve(value)
    }

    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) {
        finish(null)
        return
      }
      try {
        finish(await readTextFile(file))
      } catch {
        finish(null)
      }
    }
    // 取消 == 没选文件，和「选了但读不出来」一样返回 null；所有调用方都写了
    // `if (text == null) return`，不会把它当成空字符串覆盖已填好的私钥。
    input.addEventListener('cancel', () => finish(null))

    document.body.appendChild(input)
    input.click()
  })
}
