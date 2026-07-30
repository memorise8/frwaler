import { loadLibertreeReadOnlyDataPaths } from "../src/lib/data-path.ts"

const main = (): void => {
  try {
    const paths = loadLibertreeReadOnlyDataPaths(process.env)
    console.log(JSON.stringify({ status: "PASS", dbPath: paths.dbPath, blobRoot: paths.blobRoot }))
  } catch (error) {
    if (error instanceof Error) {
      console.log(JSON.stringify({ status: "REJECT", error: error.message }))
      process.exitCode = 1
      return
    }
    throw error
  }
}

main()
