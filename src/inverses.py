import numpy as np

def inverse( gt_mode, pred_df, alpha, beta, min_step=0.01 ):
    inverse_function = {
        'siren': inv_siren,
        'cosine': inv_cosine,
        'squared': inv_squared,
        'tanh': inv_tanh
    }
    return inverse_function[gt_mode](pred_df, alpha, beta, min_step)

def inv_squared( pred_df, alpha, beta, min_step ):
    inverse = np.ones_like( pred_df ) * min_step
    np.sqrt( pred_df, out=inverse, where=pred_df > 0)
    inverse /= np.sqrt(alpha)

    return inverse

def inv_cosine( pred_df, alpha, beta, min_step ):
    inverse = np.ones_like( pred_df ) * min_step
    np.arccos( 1 - pred_df / beta, out=inverse, where=pred_df > 0 )
    return inverse / alpha

def inv_tanh( pred_df, alpha, beta, min_step ):
    # es muy parecida al modulo. devuelvo eso
    return np.where( pred_df > 0, pred_df, np.ones_like(pred_df) * min_step ) 

def inv_siren( pred_df, alpha, beta, min_step ):
    return np.where( pred_df > 0, pred_df, np.ones_like(pred_df) * min_step ) 