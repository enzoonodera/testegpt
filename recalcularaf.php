function recalcularafiliados(PDO $pdo)
{
    $padrao = (float)$pdo->query("SELECT revshare_padrao FROM config LIMIT 1")->fetchColumn();

    $stmt = $pdo->query("
        SELECT 
            o.user_id,
            o.resultado,
            r.valor AS custo,
            u.indicacao,
            a.id AS afiliado_id,
            a.comissao_revshare
        FROM orders o
        JOIN raspadinhas r ON r.id = o.raspadinha_id
        JOIN usuarios u ON u.id = o.user_id
        JOIN usuarios a ON a.id = u.indicacao
        WHERE o.status = 1 AND a.influencer = 1
    ");

    $dados = [];

    while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
        $id = (int)$row['afiliado_id'];
        $pct = (float)$row['comissao_revshare'] ?: $padrao;
        $custo = (float)$row['custo'];
        $perda = $row['resultado'] === 'gain' ? -$custo : $custo;
        $comissao = ($perda * $pct) / 100;

        if (!isset($dados[$id])) $dados[$id] = 0;
        $dados[$id] += $comissao;
    }

    if (!empty($dados)) {
        $ids = implode(',', array_keys($dados));
        $pdo->exec("UPDATE usuarios SET saldo = 0 WHERE influencer = 1 AND id IN ($ids)");
    }

    $up = $pdo->prepare("UPDATE usuarios SET saldo = ? WHERE id = ?");
    foreach ($dados as $id => $valor) {
        $up->execute([round($valor, 2), $id]);
    }

    echo "finalizado";
}
