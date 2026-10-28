# Puts an HTML fragment on the Windows clipboard as rich text, so pasting into
# Teams or Outlook keeps the bold and the bullets. Plain text rides along for
# editors that can't take HTML. Usage: powershell -STA -File copy_html.ps1 <file.html>
param([Parameter(Mandatory = $true)][string]$Path)
Add-Type -AssemblyName System.Windows.Forms

$fragment = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
$utf8 = New-Object Text.UTF8Encoding($false)
$pre = "<html><body><!--StartFragment-->"
$post = "<!--EndFragment--></body></html>"
$header = "Version:0.9`r`nStartHTML:{0:D10}`r`nEndHTML:{1:D10}`r`nStartFragment:{2:D10}`r`nEndFragment:{3:D10}`r`n"
# CF_HTML offsets are UTF-8 byte counts; the header length is fixed by the D10 padding.
$headerLen = $utf8.GetByteCount(($header -f 0, 0, 0, 0))
$startHtml = $headerLen
$startFrag = $startHtml + $utf8.GetByteCount($pre)
$endFrag = $startFrag + $utf8.GetByteCount($fragment)
$endHtml = $endFrag + $utf8.GetByteCount($post)
$cfHtml = ($header -f $startHtml, $endHtml, $startFrag, $endFrag) + $pre + $fragment + $post

$plain = [Net.WebUtility]::HtmlDecode(($fragment -replace '<li>', '- ' -replace '</(p|li)>', "`r`n" -replace '<[^>]+>', ''))
$data = New-Object Windows.Forms.DataObject
# A MemoryStream keeps the UTF-8 bytes exact; a string would be re-encoded.
$data.SetData([Windows.Forms.DataFormats]::Html, (New-Object IO.MemoryStream(, $utf8.GetBytes($cfHtml))))
$data.SetData([Windows.Forms.DataFormats]::UnicodeText, $plain)
[Windows.Forms.Clipboard]::SetDataObject($data, $true)
"Copied as rich text: paste with Ctrl+V"
