# symmetrical-parakeet
epub builder

Aplikasi web kecil untuk menyusun EPUB, termasuk mengonversi PDF menjadi EPUB
untuk e-reader yang hanya bisa membaca EPUB (misalnya Xteink X4).

## Menjalankan

```
python -m pip install -r requirements.txt
python app.py
```

Buka http://localhost:5000.

## Konversi PDF ke EPUB

1. Di halaman utama, bagian **Impor dari PDF**, pilih file PDF.
2. Pilih metode:
   - **Otomatis**: mengambil teks dari PDF, dan memakai OCR hanya untuk halaman
     hasil scan (atau halaman yang teksnya rusak). Pilihan ini cocok untuk kebanyakan PDF.
   - **Ekstrak teks saja**: untuk PDF digital (hasil ekspor Word, dsb.). Paling cepat.
   - **OCR semua halaman**: untuk PDF hasil scan, atau bila teks bawaan PDF kacau.
3. Klik **Konversi ke Draf**, tunggu sampai selesai, lalu **Unduh EPUB**.
   Hasilnya juga tersimpan sebagai draf, jadi judul, penulis, isi bab, dan cover
   bisa diperiksa dan diperbaiki dulu (misalnya salah baca OCR) sebelum diunduh ulang.

Yang dilakukan konverter:

- menyambung kembali baris menjadi paragraf, termasuk kata yang terpotong tanda
  hubung di akhir baris (kata ulang seperti "anak-anak" tetap utuh);
- membuang header/footer berulang, nomor halaman, dan watermark;
- membagi buku per bab memakai bookmark PDF, atau judul seperti "BAB 1", "BAB IV",
  "Prolog", dan judul berhuruf besar; bila tidak ada, dibagi per 15 halaman;
- halaman pertama dijadikan cover (lebar 480 px, sesuai layar Xteink X4); bisa dimatikan.

### Data bahasa OCR

OCR memakai Tesseract yang sudah ada di dalam PyMuPDF, jadi tidak perlu memasang
program lain. Saat OCR pertama kali dipakai, data bahasa (`ind` sekitar 1 MB,
`eng` sekitar 4 MB) diunduh otomatis dari
[tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) ke folder `tessdata/`.

Tanpa internet, salin `ind.traineddata` / `eng.traineddata` secara manual ke folder
`tessdata/`. Folder dari `TESSDATA_PREFIX` atau instalasi Tesseract di
`C:\Program Files\Tesseract-OCR\tessdata` juga dipakai bila tersedia.

OCR berjalan paralel di beberapa core CPU; buku scan 300 halaman biasanya selesai
dalam beberapa menit.

### Keterbatasan

- Yang dikonversi hanya teks. Gambar, tabel, dan rumus di dalam PDF tidak ikut.
- Tata letak yang rumit (lebih dari dua kolom, kotak-kotak teks) bisa tersusun kurang rapi.
- Hasil OCR bergantung pada kualitas scan; periksa draf sebelum diunduh.
- PDF yang dikunci kata sandi harus dibuka kuncinya dulu.
