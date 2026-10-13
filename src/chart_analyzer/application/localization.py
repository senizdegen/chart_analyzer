"""Localized error messages.

These are fixed product texts, not AI output, so they live in a plain dictionary:
every ``ErrorCode`` has a message in every supported ``Language`` (a unit test enforces it).
The client always receives the stable code together with the ready-to-show text.
"""

from chart_analyzer.domain.enums import ErrorCode, Language

_L = Language
_E = ErrorCode

_MESSAGES: dict[ErrorCode, dict[Language, str]] = {
    _E.MISSING_USER_ID: {
        _L.EN: "The x-user-id header is required.",
        _L.ES: "Se requiere el encabezado x-user-id.",
        _L.PT: "O cabeçalho x-user-id é obrigatório.",
        _L.DE: "Der Header x-user-id ist erforderlich.",
        _L.TR: "x-user-id başlığı zorunludur.",
        _L.FR: "L'en-tête x-user-id est obligatoire.",
        _L.IT: "L'intestazione x-user-id è obbligatoria.",
        _L.ID: "Header x-user-id wajib diisi.",
        _L.FIL: "Kailangan ang x-user-id header.",
    },
    _E.INVALID_IMAGE: {
        _L.EN: "Please upload a valid image file (PNG, JPEG or WebP).",
        _L.ES: "Sube un archivo de imagen válido (PNG, JPEG o WebP).",
        _L.PT: "Envie um arquivo de imagem válido (PNG, JPEG ou WebP).",
        _L.DE: "Bitte lade eine gültige Bilddatei hoch (PNG, JPEG oder WebP).",
        _L.TR: "Lütfen geçerli bir görsel dosyası yükleyin (PNG, JPEG veya WebP).",
        _L.FR: "Veuillez importer un fichier image valide (PNG, JPEG ou WebP).",
        _L.IT: "Carica un file immagine valido (PNG, JPEG o WebP).",
        _L.ID: "Unggah file gambar yang valid (PNG, JPEG, atau WebP).",
        _L.FIL: "Mag-upload ng wastong image file (PNG, JPEG o WebP).",
    },
    _E.IMAGE_TOO_LARGE: {
        _L.EN: "The image is too large. Please upload a smaller file.",
        _L.ES: "La imagen es demasiado grande. Sube un archivo más pequeño.",
        _L.PT: "A imagem é grande demais. Envie um arquivo menor.",
        _L.DE: "Das Bild ist zu groß. Bitte lade eine kleinere Datei hoch.",
        _L.TR: "Görsel çok büyük. Lütfen daha küçük bir dosya yükleyin.",
        _L.FR: "L'image est trop volumineuse. Veuillez importer un fichier plus petit.",
        _L.IT: "L'immagine è troppo grande. Carica un file più piccolo.",
        _L.ID: "Gambar terlalu besar. Unggah file yang lebih kecil.",
        _L.FIL: "Masyadong malaki ang larawan. Mag-upload ng mas maliit na file.",
    },
    _E.UNSUPPORTED_LANGUAGE: {
        _L.EN: "This language is not supported.",
        _L.ES: "Este idioma no es compatible.",
        _L.PT: "Este idioma não é suportado.",
        _L.DE: "Diese Sprache wird nicht unterstützt.",
        _L.TR: "Bu dil desteklenmiyor.",
        _L.FR: "Cette langue n'est pas prise en charge.",
        _L.IT: "Questa lingua non è supportata.",
        _L.ID: "Bahasa ini tidak didukung.",
        _L.FIL: "Hindi suportado ang wikang ito.",
    },
    _E.QUOTA_EXCEEDED: {
        _L.EN: "You have reached your daily analysis limit. It resets at midnight UTC.",
        _L.ES: "Has alcanzado tu límite diario de análisis. Se restablece a medianoche UTC.",
        _L.PT: "Você atingiu seu limite diário de análises. Ele é renovado à meia-noite UTC.",
        _L.DE: "Du hast dein tägliches Analyselimit erreicht. "
        "Es wird um Mitternacht UTC zurückgesetzt.",
        _L.TR: "Günlük analiz limitinize ulaştınız. Limit UTC gece yarısında sıfırlanır.",
        _L.FR: "Vous avez atteint votre limite quotidienne d'analyses. "
        "Elle est réinitialisée à minuit UTC.",
        _L.IT: "Hai raggiunto il limite giornaliero di analisi. Si azzera a mezzanotte UTC.",
        _L.ID: "Anda telah mencapai batas analisis harian. Batas direset pada tengah malam UTC.",
        _L.FIL: "Naabot mo na ang iyong pang-araw-araw na limitasyon sa pagsusuri. "
        "Mare-reset ito sa hatinggabi UTC.",
    },
    _E.TOO_MANY_ACTIVE_ANALYSES: {
        _L.EN: "Too many analyses are in progress. Please wait for one to finish.",
        _L.ES: "Hay demasiados análisis en curso. Espera a que termine uno.",
        _L.PT: "Há análises demais em andamento. Aguarde uma terminar.",
        _L.DE: "Es laufen zu viele Analysen. Bitte warte, bis eine abgeschlossen ist.",
        _L.TR: "Çok fazla analiz devam ediyor. Lütfen birinin bitmesini bekleyin.",
        _L.FR: "Trop d'analyses sont en cours. Veuillez attendre la fin de l'une d'elles.",
        _L.IT: "Ci sono troppe analisi in corso. Attendi che una termini.",
        _L.ID: "Terlalu banyak analisis sedang berjalan. Tunggu hingga salah satunya selesai.",
        _L.FIL: "Masyadong maraming pagsusuri ang tumatakbo. Hintaying matapos ang isa.",
    },
    _E.ANALYSIS_NOT_FOUND: {
        _L.EN: "Analysis not found.",
        _L.ES: "Análisis no encontrado.",
        _L.PT: "Análise não encontrada.",
        _L.DE: "Analyse nicht gefunden.",
        _L.TR: "Analiz bulunamadı.",
        _L.FR: "Analyse introuvable.",
        _L.IT: "Analisi non trovata.",
        _L.ID: "Analisis tidak ditemukan.",
        _L.FIL: "Hindi nahanap ang pagsusuri.",
    },
    _E.VALIDATION_ERROR: {
        _L.EN: "The request is invalid.",
        _L.ES: "La solicitud no es válida.",
        _L.PT: "A solicitação é inválida.",
        _L.DE: "Die Anfrage ist ungültig.",
        _L.TR: "İstek geçersiz.",
        _L.FR: "La requête est invalide.",
        _L.IT: "La richiesta non è valida.",
        _L.ID: "Permintaan tidak valid.",
        _L.FIL: "Hindi wasto ang kahilingan.",
    },
    _E.UNCLEAR_CHART: {
        _L.EN: "Please upload a clear trading chart.",
        _L.ES: "Sube un gráfico de trading claro.",
        _L.PT: "Envie um gráfico de trading nítido.",
        _L.DE: "Bitte lade einen klaren Trading-Chart hoch.",
        _L.TR: "Lütfen net bir işlem grafiği yükleyin.",
        _L.FR: "Veuillez importer un graphique de trading net.",
        _L.IT: "Carica un grafico di trading chiaro.",
        _L.ID: "Unggah grafik trading yang jelas.",
        _L.FIL: "Mag-upload ng malinaw na trading chart.",
    },
    _E.ASSET_NOT_FOUND: {
        _L.EN: "We couldn't find market data for this asset. "
        "Please upload a chart with a clearly visible ticker.",
        _L.ES: "No encontramos datos de mercado para este activo. "
        "Sube un gráfico con el ticker claramente visible.",
        _L.PT: "Não encontramos dados de mercado para este ativo. "
        "Envie um gráfico com o ticker bem visível.",
        _L.DE: "Wir konnten keine Marktdaten für diesen Wert finden. "
        "Bitte lade einen Chart mit gut sichtbarem Ticker hoch.",
        _L.TR: "Bu varlık için piyasa verisi bulamadık. "
        "Lütfen sembolü net görünen bir grafik yükleyin.",
        _L.FR: "Nous n'avons trouvé aucune donnée de marché pour cet actif. "
        "Veuillez importer un graphique dont le ticker est bien visible.",
        _L.IT: "Non abbiamo trovato dati di mercato per questo asset. "
        "Carica un grafico con il ticker ben visibile.",
        _L.ID: "Kami tidak menemukan data pasar untuk aset ini. "
        "Unggah grafik dengan ticker yang terlihat jelas.",
        _L.FIL: "Wala kaming nahanap na market data para sa asset na ito. "
        "Mag-upload ng chart na malinaw ang ticker.",
    },
    _E.MARKET_DATA_UNAVAILABLE: {
        _L.EN: "Market data is temporarily unavailable. Please try again later.",
        _L.ES: "Los datos de mercado no están disponibles temporalmente. Inténtalo más tarde.",
        _L.PT: "Os dados de mercado estão temporariamente indisponíveis. Tente mais tarde.",
        _L.DE: "Marktdaten sind vorübergehend nicht verfügbar. Bitte versuche es später erneut.",
        _L.TR: "Piyasa verileri geçici olarak kullanılamıyor. Lütfen daha sonra tekrar deneyin.",
        _L.FR: "Les données de marché sont temporairement indisponibles. "
        "Veuillez réessayer plus tard.",
        _L.IT: "I dati di mercato non sono temporaneamente disponibili. Riprova più tardi.",
        _L.ID: "Data pasar sementara tidak tersedia. Coba lagi nanti.",
        _L.FIL: "Pansamantalang hindi available ang market data. Subukan muli mamaya.",
    },
    _E.AI_UNAVAILABLE: {
        _L.EN: "The analysis service is temporarily unavailable. Please try again later.",
        _L.ES: "El servicio de análisis no está disponible temporalmente. Inténtalo más tarde.",
        _L.PT: "O serviço de análise está temporariamente indisponível. Tente mais tarde.",
        _L.DE: "Der Analysedienst ist vorübergehend nicht verfügbar. "
        "Bitte versuche es später erneut.",
        _L.TR: "Analiz servisi geçici olarak kullanılamıyor. Lütfen daha sonra tekrar deneyin.",
        _L.FR: "Le service d'analyse est temporairement indisponible. "
        "Veuillez réessayer plus tard.",
        _L.IT: "Il servizio di analisi non è temporaneamente disponibile. Riprova più tardi.",
        _L.ID: "Layanan analisis sementara tidak tersedia. Coba lagi nanti.",
        _L.FIL: "Pansamantalang hindi available ang serbisyo ng pagsusuri. Subukan muli mamaya.",
    },
    _E.INVALID_AI_RESPONSE: {
        _L.EN: "We couldn't analyze this chart. Please try again.",
        _L.ES: "No pudimos analizar este gráfico. Inténtalo de nuevo.",
        _L.PT: "Não conseguimos analisar este gráfico. Tente novamente.",
        _L.DE: "Wir konnten diesen Chart nicht analysieren. Bitte versuche es erneut.",
        _L.TR: "Bu grafiği analiz edemedik. Lütfen tekrar deneyin.",
        _L.FR: "Nous n'avons pas pu analyser ce graphique. Veuillez réessayer.",
        _L.IT: "Non siamo riusciti ad analizzare questo grafico. Riprova.",
        _L.ID: "Kami tidak dapat menganalisis grafik ini. Silakan coba lagi.",
        _L.FIL: "Hindi namin nasuri ang chart na ito. Pakisubukang muli.",
    },
    _E.INTERNAL_ERROR: {
        _L.EN: "Something went wrong. Please try again later.",
        _L.ES: "Algo salió mal. Inténtalo más tarde.",
        _L.PT: "Algo deu errado. Tente novamente mais tarde.",
        _L.DE: "Etwas ist schiefgelaufen. Bitte versuche es später erneut.",
        _L.TR: "Bir şeyler ters gitti. Lütfen daha sonra tekrar deneyin.",
        _L.FR: "Une erreur s'est produite. Veuillez réessayer plus tard.",
        _L.IT: "Qualcosa è andato storto. Riprova più tardi.",
        _L.ID: "Terjadi kesalahan. Coba lagi nanti.",
        _L.FIL: "May nangyaring mali. Subukan muli mamaya.",
    },
}


def message_for(code: ErrorCode, language: Language) -> str:
    """Text of ``code`` in ``language``."""
    return _MESSAGES[code][language]
