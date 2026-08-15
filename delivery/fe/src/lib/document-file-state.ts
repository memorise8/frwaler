const fileExtension = (filename: string | null): string | null => {
  if (!filename) return null;
  const match = /\.([a-z0-9]+)$/i.exec(filename.trim());
  return match ? match[1]!.toLowerCase() : null;
};

// files.original_filename is the name of the source file as crawled -- it
// records what the file is called, not whether it was ever downloaded and
// stored. Showing it next to "PDF 미확보" without this distinction reads as
// a contradiction: the screen looks like it has the file (it knows the
// name) while also saying it doesn't. When the source file's own extension
// isn't .pdf (an .xlsx report, for instance), that alone explains why no
// PDF was ever stored; when it is .pdf (or unknown), the absence is just
// "not fetched yet" and should read that way instead of implying a fault.
export const pdfMissingReason = (originalFilename: string | null): string => {
  const ext = fileExtension(originalFilename);
  if (ext && ext !== "pdf") return `원본이 PDF가 아닌 .${ext} 파일이라 PDF를 확보하지 않았습니다.`;
  return "원문에서 PDF를 아직 확보하지 못했습니다.";
};

// Text extraction only ever runs against a stored PDF, so "no PDF" and "PDF
// but not yet extracted" are different situations worth saying apart.
export const textMissingReason = (hasPdf: boolean): string =>
  hasPdf ? "PDF는 있지만 텍스트를 추출하지 못했습니다." : "PDF가 없어 텍스트를 추출하지 않았습니다.";
