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
    input.onchange = async () => {
      const file = input.files?.[0]
      document.body.removeChild(input)
      if (!file) {
        resolve(null)
        return
      }
      try {
        resolve(await readTextFile(file))
      } catch {
        resolve(null)
      }
    }
    document.body.appendChild(input)
    input.click()
  })
}
